"""Small Telegram Bot API client using only Python's standard library."""

import json
import re
import urllib.error
import urllib.parse
import urllib.request


class TelegramAPIError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str) -> None:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("TELEGRAM_BOT_TOKEN format is invalid")
        encoded_token = urllib.parse.quote(token, safe=":")
        self._base_url = f"https://api.telegram.org/bot{encoded_token}"

    def call(self, method: str, payload: dict, timeout: int = 40) -> dict:
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            # Never include the URL or request body; the URL contains the bot token.
            raise TelegramAPIError(type(error).__name__) from None
        if not body.get("ok"):
            raise TelegramAPIError(f"Telegram API returned error code {body.get('error_code', 'unknown')}")
        return body["result"]

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self.call("sendMessage", payload)

    def answer_callback(self, callback_query_id: str) -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_query_id}, timeout=10)
