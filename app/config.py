"""Application configuration loaded from environment variables (and optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv_file(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass(frozen=True)
class Settings:
    db_path: Path
    menu_path: Path
    kitchen_secret: str
    public_base_url: str
    payment_info_text: str
    line_channel_secret: str
    line_channel_access_token: str
    owner_line_user_id: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_reasoning_effort: str
    # v2 payments (all optional: unset means the feature is off and v1 behavior applies)
    promptpay_id: str = ""
    slipok_api_key: str = ""
    slipok_branch_id: str = ""


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    _load_dotenv_file(root / ".env")
    db_raw = os.environ.get("DB_PATH", "shop.db")
    menu_raw = os.environ.get("MENU_PATH", "menu.yaml")
    db_path = Path(db_raw) if Path(db_raw).is_absolute() else root / db_raw
    menu_path = Path(menu_raw) if Path(menu_raw).is_absolute() else root / menu_raw
    return Settings(
        db_path=db_path,
        menu_path=menu_path,
        kitchen_secret=os.environ.get("KITCHEN_SECRET", "").strip(),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        payment_info_text=os.environ.get("PAYMENT_INFO_TEXT", "").strip(),
        line_channel_secret=os.environ.get("LINE_CHANNEL_SECRET", "").strip(),
        line_channel_access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip(),
        owner_line_user_id=os.environ.get("OWNER_LINE_USER_ID", "").strip(),
        llm_base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_model=os.environ.get("LLM_MODEL", "anthropic/claude-haiku-5.5").strip(),
        llm_reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", "low").strip(),
        promptpay_id=os.environ.get("PROMPTPAY_ID", "").strip(),
        slipok_api_key=os.environ.get("SLIPOK_API_KEY", "").strip(),
        slipok_branch_id=os.environ.get("SLIPOK_BRANCH_ID", "").strip(),
    )
