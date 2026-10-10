"""Initialize the first admin offline. Passwords are read without echo."""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from filelock import FileLock
from app.config import Settings
from app.security import Security, UserInput
from app.store import Store


def main():
    parser = argparse.ArgumentParser(description="停服后初始化首位管理员；无默认密码")
    parser.add_argument("--db", type=Path, default=Settings().resolved_db_path)
    parser.add_argument("--username", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Password (12+ characters): ")
    if password != getpass.getpass("Confirm password: "):
        parser.error("两次密码不一致")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(args.db.resolve()) + ".service.lock", timeout=0):
        user = Security(Store(args.db)).create_user(UserInput(username=args.username, display_name=args.name, password=password, role="admin"), bootstrap=True)
    print(f'管理员已创建：{user["username"]}')


if __name__ == "__main__":
    main()
