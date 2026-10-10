from pathlib import Path
from typing import Literal
from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUZHANG_", env_file=ROOT / ".env", extra="ignore")
    db_path: Path = ROOT / "data" / "suzhang.db"
    llm_base_url: str = ""
    llm_model: str = "Qwen3-32B"
    llm_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "BGE-M3"
    embedding_api_key: str = ""
    rerank_url: str = ""
    rerank_api_key: str = ""
    model_timeout: float = 20
    seed_demo: bool = True
    api_token: str = ""
    auth_mode: Literal["legacy", "accounts"] = "legacy"
    cookie_secure: bool = True
    ocr_url: str = ""
    ocr_api_key: str = ""
    ocr_timeout: float = Field(default=120, ge=1, le=600)
    ocr_dpi: int = Field(default=150, ge=72, le=300)
    ocr_device: str = "cpu"
    ocr_paddlex_config: str = ""
    ocr_model_version: str = Field(default="",max_length=64)

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path if self.db_path.is_absolute() else ROOT / self.db_path
