"""AT command allow-list.

Every command sent to the modem passes through ``check``. Anything not matched
by an allowed pattern raises ``ForbiddenCommand`` *before* it reaches USB.

The list contains only read-only queries plus two harmless session settings:
selecting PDU mode and selecting which message storage to read from. There is
deliberately no pattern for sending (CMGS/CMSS), writing (CMGW), deleting
(CMGD), eSIM/APDU access (CSIM/CRSM/CGLA/QESIM...), or vendor configuration
(QCFG/QPRTPARA/...). Identifiers such as IMEI/IMSI/ICCID are not queried.
"""

from __future__ import annotations

import re

_ALLOWED = [
    r"AT",
    r"ATE0",
    r"ATI",
    r"AT\+CPIN\?",
    r"AT\+CSQ",
    r"AT\+CREG\?",
    r"AT\+COPS\?",
    r"AT\+CMGF=0",
    r"AT\+CPMS\?",
    r'AT\+CPMS="(?:ME|SM)"(?:,"(?:ME|SM)"){0,2}',
    r"AT\+CMGL=4",
]
_PATTERNS = [re.compile(rf"^{p}$") for p in _ALLOWED]


class ForbiddenCommand(Exception):
    """Raised when code tries to send an AT command that is not allow-listed."""


def check(cmd: str) -> str:
    """Return ``cmd`` normalised (stripped, no CR/LF) if allowed, else raise."""
    if any(ch in cmd for ch in "\r\n\x00\x1a\x1b"):
        raise ForbiddenCommand("control characters are not allowed in AT commands")
    norm = cmd.strip()
    for pat in _PATTERNS:
        if pat.match(norm):
            return norm
    raise ForbiddenCommand(f"AT command not allow-listed: {norm.split('=')[0][:16]}")
