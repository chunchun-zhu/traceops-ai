"""Offline paired SQLite backups. Restore only publishes a new directory."""
import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

from filelock import FileLock
from .store import now


FILES = ("suzhang.db", "suzhang.db.checkpoints")


def remove_staging(staging, parent):
    target = staging.resolve()
    if target.parent != parent.resolve() or not target.name.startswith((".backup-", ".restore-")):
        raise ValueError("暂存目录不在预期范围")
    shutil.rmtree(target)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_db(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性校验失败")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("数据库外键校验失败")


def validate(bundle):
    bundle = Path(bundle).resolve()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or set(manifest.get("files", {})) != set(FILES):
        raise ValueError("备份清单格式或文件集合错误")
    for name in FILES:
        path = bundle / name
        if path.is_symlink() or digest(path) != manifest["files"][name]:
            raise ValueError("备份校验和不匹配")
        check_db(path)
    with closing(sqlite3.connect((bundle / FILES[0]).as_uri() + "?mode=ro", uri=True)) as db:
        required = {"sessions", "events", "jobs", "documents", "standards", "alarms"}
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise ValueError("业务数据库缺少必要表")
    with closing(sqlite3.connect((bundle / FILES[1]).as_uri() + "?mode=ro", uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"checkpoints", "writes"} <= tables:
            raise ValueError("检查点数据库缺少必要表；请先正常启动服务")
    return manifest


def backup(db_path, destination):
    source, destination = Path(db_path).resolve(), Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("目标目录已存在，备份不会覆盖")
    if not source.is_file() or not Path(str(source) + ".checkpoints").is_file():
        raise ValueError("业务库与检查点库必须同时存在")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        for suffix in (".service.lock", ".workflow.lock", ".tasks.lock"):
            locks.enter_context(FileLock(str(source) + suffix, timeout=0))
        staging = Path(tempfile.mkdtemp(prefix=".backup-", dir=destination.parent))
        try:
            for suffix, name in zip(("", ".checkpoints"), FILES):
                with closing(sqlite3.connect(Path(str(source) + suffix).as_uri() + "?mode=ro", uri=True)) as src:
                    with closing(sqlite3.connect(staging / name)) as dst:
                        src.backup(dst)
            manifest = {"format": 1, "created_at": now(), "application_version": "0.5.0",
                        "files": {name: digest(staging / name) for name in FILES}}
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            validate(staging)
            staging.rename(destination)
        finally:
            if staging.exists():
                remove_staging(staging, destination.parent)
    return manifest


def restore(bundle, destination):
    bundle, destination = Path(bundle).resolve(), Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("恢复目标必须是尚不存在的新目录")
    validate(bundle)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    try:
        for name in (*FILES, "manifest.json"):
            shutil.copyfile(bundle / name, staging / name)
        validate(staging)
        # Restored credentials survive, but existing login sessions must be reissued.
        with closing(sqlite3.connect(staging / FILES[0])) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='auth_sessions'").fetchone():
                db.execute("DELETE FROM auth_sessions")
                db.commit()
        (staging / "manifest.json").unlink()
        staging.rename(destination)
    finally:
        if staging.exists():
            remove_staging(staging, destination.parent)
    return destination / FILES[0]
