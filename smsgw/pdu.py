"""SMS PDU (3GPP TS 23.040) decoder for SMS-DELIVER messages.

Only decoding is implemented. There is no encoder: this gateway cannot build
outgoing messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)
GSM7_EXT = {
    0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\",
    0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "€",
}
assert len(GSM7_BASIC) == 128

_ADDR_DIGITS = "0123456789*#abc\x00"


class PduError(ValueError):
    pass


@dataclass
class Sms:
    sender: str
    timestamp: str  # ISO 8601 with offset when the network provided one
    text: str
    concat: Optional[Tuple[int, int, int]]  # (reference, total, sequence) or None
    kind: str = "deliver"


def _unpack_septets(data: bytes, count: int, skip_bits: int = 0) -> list:
    out = []
    for i in range(count):
        bit = skip_bits + i * 7
        byte, shift = divmod(bit, 8)
        if byte >= len(data):
            raise PduError("GSM7 data truncated")
        val = data[byte] >> shift
        if shift > 1 and byte + 1 < len(data):
            val |= data[byte + 1] << (8 - shift)
        out.append(val & 0x7F)
    return out


def _gsm7_text(septets: list) -> str:
    chars = []
    esc = False
    for s in septets:
        if esc:
            chars.append(GSM7_EXT.get(s, " "))
            esc = False
        elif s == 0x1B:
            esc = True
        else:
            chars.append(GSM7_BASIC[s])
    return "".join(chars)


def _read_address(buf: bytes, pos: int) -> Tuple[str, int]:
    ndigits = buf[pos]
    toa = buf[pos + 1]
    nbytes = (ndigits + 1) // 2
    raw = buf[pos + 2 : pos + 2 + nbytes]
    if len(raw) != nbytes:
        raise PduError("address truncated")
    ton = (toa >> 4) & 0x07
    if ton == 0b101:  # alphanumeric sender
        septets = _unpack_septets(raw, ndigits * 4 // 7)
        return _gsm7_text(septets), pos + 2 + nbytes
    digits = []
    for b in raw:
        for nib in (b & 0x0F, b >> 4):
            digits.append(_ADDR_DIGITS[nib] if nib < 0x0F else "")
    num = "".join(digits)[:ndigits]
    if ton == 0b001 and num:
        num = "+" + num
    return num, pos + 2 + nbytes


def _bcd(b: int) -> int:
    return (b & 0x0F) * 10 + (b >> 4)


def _timestamp(raw: bytes) -> str:
    try:
        yy, mo, dd, hh, mi, ss = (_bcd(x) for x in raw[:6])
        tzb = raw[6]
        neg = bool(tzb & 0x08)
        quarters = ((tzb & 0x07) * 10) + (tzb >> 4)
        offset = timedelta(minutes=15 * quarters) * (-1 if neg else 1)
        dt = datetime(2000 + yy, mo, dd, hh, mi, ss, tzinfo=timezone(offset))
        return dt.isoformat()
    except (ValueError, IndexError):
        return ""


def _alphabet(dcs: int) -> str:
    if (dcs & 0xC0) == 0x00:
        return ("gsm7", "8bit", "ucs2", "gsm7")[(dcs >> 2) & 3]
    if (dcs & 0xF0) == 0xF0:
        return "8bit" if dcs & 0x04 else "gsm7"
    if (dcs & 0xF0) in (0xC0, 0xD0):
        return "gsm7"
    if (dcs & 0xF0) == 0xE0:
        return "ucs2"
    return "gsm7"


def decode(pdu_hex: str) -> Sms:
    try:
        buf = bytes.fromhex(pdu_hex.strip())
    except ValueError as e:
        raise PduError("invalid hex") from e
    try:
        pos = 0
        smsc_len = buf[pos]
        pos += 1 + smsc_len
        first = buf[pos]
        pos += 1
        mti = first & 0x03
        if mti != 0:
            return Sms(sender="", timestamp="", text="", concat=None, kind="other")
        udhi = bool(first & 0x40)
        sender, pos = _read_address(buf, pos)
        pos += 1  # PID
        dcs = buf[pos]
        pos += 1
        ts = _timestamp(buf[pos : pos + 7])
        pos += 7
        udl = buf[pos]
        pos += 1
        ud = buf[pos:]
    except IndexError as e:
        raise PduError("PDU truncated") from e

    alphabet = _alphabet(dcs)
    concat = None
    header_len = 0
    if udhi:
        if not ud:
            raise PduError("UDH missing")
        udhl = ud[0]
        header_len = 1 + udhl
        if header_len > len(ud):
            raise PduError("UDH truncated")
        i = 1
        while i + 1 < header_len:
            iei, ielen = ud[i], ud[i + 1]
            if i + 2 + ielen > header_len:
                raise PduError("UDH element overruns the header")
            body = ud[i + 2 : i + 2 + ielen]
            if iei == 0x00 and ielen == 3:
                concat = (body[0], body[1], body[2])
            elif iei == 0x08 and ielen == 4:
                concat = ((body[0] << 8) | body[1], body[2], body[3])
            i += 2 + ielen

    if alphabet == "gsm7":
        header_bits = header_len * 8
        skip_septets = (header_bits + 6) // 7
        if udl < skip_septets:
            raise PduError("UDL smaller than header")
        # UDL counts septets including the (padded) header, so unpack all of
        # them from bit 0 and drop the ones the header occupies.
        text = _gsm7_text(_unpack_septets(ud, udl)[skip_septets:])
    elif alphabet == "ucs2":
        text = ud[header_len:udl].decode("utf-16-be", "replace")
    else:
        data = ud[header_len:udl]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    return Sms(sender=sender, timestamp=ts, text=text, concat=concat)
