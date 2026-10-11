"""Minimal Telegram Bot API notifier using only the Python standard library."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def send_message(token: str, chat_id: str, text: str) -> None:
    if not token.strip() or not chat_id.strip():
        raise ValueError("Telegram token and chat ID must both be configured")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"Telegram delivery failed: {exc}") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram API rejected the signal message")
