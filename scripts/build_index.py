"""增量构建；不会部署模型。项目根目录运行 python scripts/build_index.py。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.models import ModelGateway
from app.store import Store
from app.indexing import PersistentIndex

if __name__ == "__main__":
    settings = Settings()
    index = PersistentIndex(Store(settings.resolved_db_path), ModelGateway(settings))
    print(json.dumps(index.build(), ensure_ascii=False, indent=2))
