from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_user_id: int
    support_admin_username: str
    db_path: str
    polling_timeout: int = 30


def load_settings() -> Settings:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    admin_raw = os.environ.get("ADMIN_USER_ID", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required.")
    if not admin_raw.isdigit():
        raise RuntimeError("ADMIN_USER_ID must be a numeric Telegram user ID.")
    return Settings(
        bot_token=token,
        admin_user_id=int(admin_raw),
        support_admin_username=os.environ.get("SUPPORT_ADMIN_USERNAME", "Sojib31_4").lstrip("@"),
        db_path=os.environ.get("MARKETPLACE_DB_PATH", "telegram_marketplace.sqlite3"),
    )