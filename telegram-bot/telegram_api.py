from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any


class TelegramApi:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **payload: Any) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(
            {
                key: json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else str(value)
                for key, value in payload.items()
                if value is not None
            }
        ).encode()
        request = urllib.request.Request(self.base_url + method, data=encoded)
        with urllib.request.urlopen(request, timeout=45) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error: {body.get('description', 'unknown error')}")
        return body

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        try:
            return self.call("getUpdates", offset=offset, timeout=timeout).get("result", [])
        except Exception as error:
            print(f"Polling error: {error}")
            time.sleep(3)
            return []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> int:
        result = self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return int(result["result"]["message_id"])

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception:
            # Telegram can reject deletion when the message is already gone or too old.
            pass

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        self.call("setMyCommands", commands=commands)

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )