"""Test-only SMS-DELIVER *encoder* used to build vectors. Not shipped in the image."""

from smsgw.pdu import GSM7_BASIC, GSM7_EXT

_EXT_REV = {v: k for k, v in GSM7_EXT.items()}


def gsm7_septets(text):
    out = []
    for ch in text:
        if ch in _EXT_REV:
            out += [0x1B, _EXT_REV[ch]]
        else:
            out.append(GSM7_BASIC.index(ch))
    return out


def pack(septets, pad_bits=0):
    bits = "0" * pad_bits + "".join(f"{s:07b}"[::-1] for s in septets)
    # septets are packed LSB-first
    n = (len(bits) + 7) // 8
    bits = bits.ljust(n * 8, "0")
    return bytes(int(bits[i * 8 : i * 8 + 8][::-1], 2) for i in range(n))


def addr_digits(num):
    intl = num.startswith("+")
    digits = num.lstrip("+")
    toa = 0x91 if intl else 0x81
    padded = digits + ("F" if len(digits) % 2 else "")
    swapped = "".join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2))
    return f"{len(digits):02X}{toa:02X}{swapped}"


def scts(y=26, mo=10, d=31, h=15, mi=30, s=0, tz_quarters=-32):
    def sw(v):
        return f"{v % 10}{v // 10}"

    neg = tz_quarters < 0
    q = abs(tz_quarters)
    tzb = ((q % 10) << 4) | (q // 10)
    if neg:
        tzb |= 0x08
    return sw(y) + sw(mo) + sw(d) + sw(h) + sw(mi) + sw(s) + f"{tzb:02X}"


def deliver(sender, text, alphabet="gsm7", concat=None, smsc="0791448720003023"):
    first = 0x04 | (0x40 if concat else 0)
    dcs = {"gsm7": 0x00, "ucs2": 0x08}[alphabet]
    header = b""
    if concat:
        ref, total, seq = concat
        header = bytes([0x05, 0x00, 0x03, ref, total, seq])
    if alphabet == "gsm7":
        sept = gsm7_septets(text)
        if header:
            hb = len(header) * 8
            pad = (7 - hb % 7) % 7
            ud = header + pack(sept, pad_bits=pad)
            udl = (hb + pad) // 7 + len(sept)
        else:
            ud = pack(sept)
            udl = len(sept)
    else:
        body = text.encode("utf-16-be")
        ud = header + body
        udl = len(ud)
    return (
        smsc + f"{first:02X}" + addr_digits(sender) + "00" + f"{dcs:02X}" + scts() + f"{udl:02X}" + ud.hex().upper()
    )
