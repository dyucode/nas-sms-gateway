"""Telegram bot client (second notification channel, for phone push).

The secret file holds two lines: line 1 = bot token, line 2 = chat id. Neither
is ever logged; errors mention only the HTTP status or exception type, never
the URL (which contains the token) or the message body.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

from .errors import DeliveryError

log = logging.getLogger("smsgw.telegram")

API = "https://api.telegram.org"
_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,50}$")
_CHAT_RE = re.compile(r"^-?\d{1,20}$")


class TelegramError(DeliveryError):
    pass


def parse_credentials(raw: str) -> Tuple[str, str]:
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if len(lines) != 2 or not _TOKEN_RE.match(lines[0]) or not _CHAT_RE.match(lines[1]):
        raise TelegramError("telegram secret must contain two lines: bot token, then chat id")
    return lines[0], lines[1]


def parse_token(raw: str) -> str:
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines or not _TOKEN_RE.match(lines[0]):
        raise TelegramError("first line of the telegram secret must be the bot token")
    return lines[0]


def _call(api: str, token: str, method: str, params: dict, timeout: float, ctx) -> dict:
    req = urllib.request.Request(
        f"{api}/bot{token}/{method}", data=urllib.parse.urlencode(params).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read(1 << 20)
    except urllib.error.HTTPError as e:
        # 400 is about this message; 401/403/429/5xx affect every message.
        raise TelegramError(f"Telegram returned HTTP {e.code}", transient=e.code != 400) from None
    except (urllib.error.URLError, OSError, ssl.SSLError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", e)
        raise TelegramError(f"cannot reach Telegram: {type(reason).__name__}") from None
    try:
        out = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        out = {}
    if not isinstance(out, dict) or out.get("ok") is not True:
        raise TelegramError("Telegram rejected the request", transient=False)
    return out


class TelegramClient:
    def __init__(self, token: str, chat_id: str, api: str = API, ca_file: Optional[str] = None,
                 timeout: float = 10.0):
        if not (_TOKEN_RE.match(token) and _CHAT_RE.match(chat_id)):
            raise TelegramError("invalid Telegram token/chat id format")
        self._token, self._chat, self._api, self._timeout = token, chat_id, api, timeout
        self._ctx = ssl.create_default_context(cafile=ca_file) if api.startswith("https") else None

    @classmethod
    def from_file(cls, path: str, ca_file: Optional[str] = None, api: str = API) -> "TelegramClient":
        try:
            with open(path, "r", encoding="utf-8") as f:
                token, chat = parse_credentials(f.read())
        except OSError as e:
            raise TelegramError(f"cannot read telegram secret: {type(e).__name__}") from None
        return cls(token, chat, api=api, ca_file=ca_file)

    def post(self, text: str, title: str = "") -> None:
        body = f"{title}\n{text}" if title else text
        _call(self._api, self._token, "sendMessage",
              {"chat_id": self._chat, "text": body[:4096] or "(empty)", "disable_web_page_preview": "true"},
              self._timeout, self._ctx)


def find_chat_ids(path: str, ca_file: Optional[str] = None, api: str = API) -> List[str]:
    """Return 'id (type, name)' for chats that have messaged the bot (token read from file)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = parse_token(f.read())
    except OSError as e:
        raise TelegramError(f"cannot read telegram secret: {type(e).__name__}") from None
    ctx = ssl.create_default_context(cafile=ca_file) if api.startswith("https") else None
    res = _call(api, token, "getUpdates", {"limit": "20"}, 10.0, ctx).get("result", [])
    seen = {}
    for u in res:
        m = u.get("message") or u.get("channel_post") or {}
        c = m.get("chat") or {}
        if "id" in c:
            name = c.get("username") or c.get("first_name") or c.get("title") or ""
            seen[str(c["id"])] = f"{c['id']} ({c.get('type','?')}, {name})"
    return list(seen.values())
