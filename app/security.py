"""Local accounts, revocable sessions and role checks; no default passwords."""
import hashlib
import secrets
import sqlite3
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field
from typing import Literal

from .store import ConflictError, now


Role = Literal["viewer", "operator", "reviewer", "admin"]


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)


class UserInput(LoginInput):
    display_name: str = Field(min_length=1, max_length=100)
    role: Role = "viewer"


class UserUpdate(BaseModel):
    role: Role
    active: bool


class PasswordInput(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class ReviewInput(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=1, max_length=2000)
    visual_checked: bool = False


def password_hash(password, salt=None):
    if len(password) < 12:
        raise ValueError("密码至少 12 个字符")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3,
                            maxmem=128 * 1024 * 1024).hex()
    return f"scrypt$32768$8$3${salt}${digest}"


def password_matches(password, encoded):
    salt = encoded.split("$")[4]
    # Login can accept short input while retaining identical expensive comparison.
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3,
                            maxmem=128 * 1024 * 1024).hex()
    return secrets.compare_digest(digest, encoded.split("$")[5])


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Security:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
                    role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_limits (
                    bucket TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, actor_id TEXT NOT NULL,
                    actor_name TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL
                );
            """)
        self.dummy = password_hash(secrets.token_urlsafe(24))

    @staticmethod
    def public(row):
        return {k: row[k] for k in ("id", "username", "display_name", "role", "active")}

    @staticmethod
    def actor(user):
        return f'{user["display_name"]} ({user["username"]})'

    def audit(self, user, action, target, db=None):
        args = (now(), user["id"], self.actor(user), action, target)
        if db is not None:
            db.execute("INSERT INTO audit_log(created_at,actor_id,actor_name,action,target) VALUES(?,?,?,?,?)", args)
        else:
            with self.store.connect() as conn:
                conn.execute("INSERT INTO audit_log(created_at,actor_id,actor_name,action,target) VALUES(?,?,?,?,?)", args)

    def users(self):
        with self.store.connect() as db:
            return [self.public(r) for r in db.execute("SELECT * FROM users ORDER BY username")]

    def create_user(self, request, actor=None, bootstrap=False):
        hashed = password_hash(request.password)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if bootstrap and db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise ConflictError("已有账号，不能重复初始化管理员")
            item = {"id": uuid4().hex, "username": request.username, "display_name": request.display_name,
                    "role": "admin" if bootstrap else request.role, "active": True}
            try:
                db.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (*item.values(), hashed))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("账号已存在") from exc
            self.audit(actor or item, "user.create", item["id"], db)
        return item

    def login(self, request, remote):
        buckets = ["user:" + token_hash(request.username), "ip:" + token_hash(remote)]
        stamp = time.time()
        # One transaction serializes limits even under concurrent login attempts.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_limits WHERE window_start<?", (stamp - 900,))
            for bucket, maximum in zip(buckets, (5, 30)):
                row = db.execute("SELECT failures FROM login_limits WHERE bucket=?", (bucket,)).fetchone()
                if row and row[0] >= maximum:
                    raise HTTPException(429, "登录尝试过多，请十五分钟后再试")
            row = db.execute("SELECT * FROM users WHERE username=?", (request.username,)).fetchone()
            valid = password_matches(request.password, row["password_hash"] if row else self.dummy)
            if not row or not row["active"] or not valid:
                for bucket in buckets:
                    db.execute("INSERT INTO login_limits VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET failures=failures+1", (bucket, stamp))
                failure = True
            else:
                failure = False
                db.execute("DELETE FROM login_limits WHERE bucket=?", (buckets[0],))
                db.execute("DELETE FROM auth_sessions WHERE expires<?", (stamp,))
                token = secrets.token_urlsafe(32)
                user = self.public(row)
                db.execute("INSERT INTO auth_sessions VALUES(?,?,?)", (token_hash(token), user["id"], stamp + 43200))
                self.audit(user, "auth.login", user["id"], db)
        if failure:
            raise HTTPException(401, "账号或密码无效")
        return token, user

    def authenticate(self, token):
        with self.store.connect() as db:
            row = db.execute("SELECT u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1", (token_hash(token or ""), time.time())).fetchone()
        if not row:
            raise HTTPException(401, "请登录，或会话已失效")
        return self.public(row)

    def logout(self, token):
        with self.store.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash(token or ""),))

    def update_user(self, user_id, request, actor):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise KeyError(user_id)
            if row["active"] and row["role"] == "admin" and (not request.active or request.role != "admin"):
                if db.execute("SELECT count(*) FROM users WHERE active=1 AND role='admin'").fetchone()[0] <= 1:
                    raise ConflictError("必须保留至少一位有效管理员")
            db.execute("UPDATE users SET role=?,active=? WHERE id=?", (request.role, int(request.active), user_id))
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            self.audit(actor, "user.update", user_id, db)
        return next(u for u in self.users() if u["id"] == user_id)

    def reset_password(self, user_id, password, actor):
        hashed = password_hash(password)
        with self.store.connect() as db:
            if db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, user_id)).rowcount != 1:
                raise KeyError(user_id)
            db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            self.audit(actor, "user.password_reset", user_id, db)
        return {"ok": True}
