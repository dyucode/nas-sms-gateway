"""Synology Chat incoming-webhook client.

The webhook URL contains the secret token, so it is read from a file (Docker
secret), never logged, and error messages only ever mention the host.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("smsgw.chat")


class ChatError(Exception):
    pass


def _is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not (addr.is_private or addr.is_loopback or addr.is_link_local):
            return False
    return bool(infos)


class ChatClient:
    def __init__(self, webhook_url: str, ca_file: Optional[str] = None, insecure_tls: bool = False, timeout: float = 10.0):
        url = webhook_url.strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ChatError("webhook URL must be http(s)://host/...")
        if "SYNO.Chat.External" not in url or "token=" not in url:
            raise ChatError("webhook URL does not look like a Synology Chat incoming webhook")
        self._url = url
        self.host = parts.hostname
        self._timeout = timeout
        self._ctx = None
        if parts.scheme == "https":
            ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
            if insecure_tls:
                if not _is_private_host(self.host):
                    raise ChatError("CHAT_INSECURE_TLS is only allowed for private/loopback addresses")
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                log.warning("TLS verification disabled for private host %s", self.host)
            self._ctx = ctx

    @staticmethod
    def sanitize(text: str) -> str:
        # Chat treats <url|label> as link markup; keep message bodies literal.
        return text.replace("<", "‹").replace(">", "›")

    def post(self, text: str) -> None:
        body = urllib.parse.urlencode({"payload": json.dumps({"text": self.sanitize(text)}, ensure_ascii=False)}).encode()
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as resp:
                data = resp.read(4096)
        except urllib.error.HTTPError as e:
            raise ChatError(f"Chat at {self.host} returned HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ssl.SSLError) as e:
            reason = getattr(e, "reason", e)
            raise ChatError(f"cannot reach Chat at {self.host}: {type(reason).__name__}") from None
        try:
            ok = json.loads(data.decode("utf-8", "replace")).get("success") is True
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            raise ChatError(f"Chat at {self.host} rejected the message")


def format_message(sender: str, ts: str, text: str, incomplete: bool = False) -> str:
    head = f"\U0001F4E9 {sender or '(unknown sender)'}"
    if ts:
        head += f"  ·  {ts.replace('T', ' ')}"
    note = "\n⚠️ 长短信缺少部分分段(以下为已收到的部分)" if incomplete else ""
    return f"{head}{note}\n{text}"
