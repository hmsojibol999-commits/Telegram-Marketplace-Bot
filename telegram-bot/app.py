"""
Render deployment wrapper for the Telegram Marketplace Bot.

This file starts the existing polling-based MarketplaceBot in a
background thread, and exposes a tiny HTTP endpoint so Render's
health check passes and UptimeRobot can keep the service awake.
"""

from __future__ import annotations

import threading

from flask import Flask

from config import load_settings
from main import MarketplaceBot


app = Flask(__name__)

_bot: MarketplaceBot | None = None
_bot_thread: threading.Thread | None = None
_startup_error: str | None = None


def _run_bot() -> None:
    """Run the Telegram bot in a background thread."""
    global _bot, _startup_error
    try:
        settings = load_settings()
        _bot = MarketplaceBot(settings)
        _bot.run()
    except Exception as error:  # noqa: BLE001
        _startup_error = str(error)
        print(f"Bot crashed: {error}")


@app.route("/")
def root():
    return "Bot is running"


@app.route("/health")
def health():
    if _startup_error:
        return {"status": "error", "error": _startup_error}, 500
    if _bot_thread is None or not _bot_thread.is_alive():
        return {"status": "starting"}, 200
    return {"status": "ok"}


def _start_bot_once() -> None:
    global _bot_thread
    if _bot_thread is None or not _bot_thread.is_alive():
        _bot_thread = threading.Thread(target=_run_bot, daemon=True)
        _bot_thread.start()


# Start the bot as soon as this module is imported by gunicorn.
_start_bot_once()
