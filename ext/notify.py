"""Telegram notification tool.

`notify_telegram` posts a message to a Telegram chat via the Bot API. It has
no dependency on R2 or xAI: credentials are read from the environment at call
time, so a container can be redeployed with new bot credentials with no code
change.
"""

import os
import re
from typing import Optional

import httpx

TELEGRAM_MESSAGE_LIMIT = 4096

_HTML_TAG_RE = re.compile(r"<[a-z/][^>]*>")


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chunk_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split text into <= `limit`-character chunks on line boundaries.

    A single line longer than `limit` is hard-split mid-line.
    """
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [text]


async def notify_telegram(
    text: str,
    parse_mode: str = "HTML",
    chat_id: Optional[str] = None,
    disable_preview: bool = True,
) -> dict:
    """Send a message to a Telegram chat via the Bot API.

    Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the environment.
    Messages over Telegram's 4096-character cap are split on line boundaries
    and sent in order as separate messages.

    Args:
        text: Message body.
        parse_mode: `"HTML"` (default), `"MarkdownV2"`, or `""` for plain text.
        chat_id: Overrides `TELEGRAM_CHAT_ID` for this call.
        disable_preview: Suppress link previews (default True).

    Returns:
        `{"ok": bool, "message_id": int | None, "chunks": int}`, plus
        `"error"` when `ok` is false.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    resolved_chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not resolved_chat_id:
        return {
            "ok": False,
            "message_id": None,
            "chunks": 0,
            "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set",
        }

    if parse_mode == "HTML" and not _HTML_TAG_RE.search(text):
        text = _escape_html(text)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    last_message_id = None
    sent = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for chunk in _chunk_text(text):
            payload = {
                "chat_id": resolved_chat_id,
                "text": chunk,
                "disable_web_page_preview": disable_preview,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                return {"ok": False, "message_id": last_message_id, "chunks": sent, "error": f"request failed: {type(exc).__name__}"}
            if response.status_code != 200:
                try:
                    error = response.json().get("description") or response.reason_phrase
                except Exception:
                    error = response.reason_phrase
                return {"ok": False, "message_id": last_message_id, "chunks": sent, "error": error}
            try:
                last_message_id = response.json().get("result", {}).get("message_id")
            except ValueError:
                return {"ok": False, "message_id": last_message_id, "chunks": sent, "error": "non-JSON reply from Telegram"}
            sent += 1

    return {"ok": True, "message_id": last_message_id, "chunks": sent}


def register(mcp):
    mcp.tool()(notify_telegram)
