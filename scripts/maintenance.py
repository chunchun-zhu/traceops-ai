import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.maintenance import backup, restore, validate


def main():
    parser = argparse.ArgumentParser(description="停服后备份；校验；恢复到新目录")
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("backup")
    save.add_argument("--db", type=Path, required=True)
    save.add_argument("--destination", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    recover = commands.add_parser("restore")
    recover.add_argument("--bundle", type=Path, required=True)
    recover.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backup":
        result = backup(args.db, args.destination)
    elif args.command == "verify":
        result = validate(args.bundle)
    else:
        result = {"restored_db": str(restore(args.bundle, args.destination))}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
