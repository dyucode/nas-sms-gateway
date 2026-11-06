"""Unit tests for the PDU decoder. Anyone can send an SMS, so all input is hostile."""
import random

import pytest

from smsgw.pdu import PduError, _alphabet, _timestamp, decode
from tests.pdu_encode import deliver, scts


def test_timestamp_with_positive_and_negative_offset():
    assert decode(deliver("+1555", "x")).timestamp == "2026-10-31T15:30:00-08:00"
    assert _timestamp(bytes.fromhex(scts(tz_quarters=32))) == "2026-10-31T15:30:00+08:00"


@pytest.mark.parametrize("raw", ["FFFFFFFFFFFFFF", "62", ""])
def test_invalid_timestamp_becomes_empty_instead_of_failing(raw):
    assert _timestamp(bytes.fromhex(raw)) == ""


@pytest.mark.parametrize("dcs,expected", [
    (0x00, "gsm7"), (0x04, "8bit"), (0x08, "ucs2"), (0x0C, "gsm7"), (0xF0, "gsm7"), (0xF4, "8bit"),
    (0xC0, "gsm7"), (0xD0, "gsm7"), (0xE0, "ucs2"), (0x40, "gsm7"),
])
def test_alphabet_selection(dcs, expected):
    assert _alphabet(dcs) == expected


def _raw(first, dcs, ud_hex, udl):
    return "00" + f"{first:02X}" + "04812143" + "00" + f"{dcs:02X}" + scts() + f"{udl:02X}" + ud_hex


def test_16_bit_concat_reference():
    # IEI 0x08: 16-bit reference 0x1234, 2 parts, part 1, then "Hi" in UCS-2
    assert decode(_raw(0x44, 0x08, "06080412340201" + "00480069", 11)).concat == (0x1234, 2, 1)


def test_8bit_body_utf8_and_latin1():
    assert decode(_raw(0x04, 0x04, "C3A9", 2)).text == "é"
    assert decode(_raw(0x04, 0x04, "E9", 1)).text == "é"


def test_status_report_is_not_treated_as_an_sms():
    assert decode("00" + "02" + "00" * 20).kind == "other"


@pytest.mark.parametrize("pdu", ["", "zz", "abc", "00", "0004"])
def test_garbage_is_a_pdu_error(pdu):
    with pytest.raises(PduError):
        decode(pdu)


def test_udh_element_overrunning_the_header_is_rejected():
    with pytest.raises(PduError):
        decode(_raw(0x44, 0x08, "020003", 3))


def test_udh_longer_than_the_message_is_rejected():
    with pytest.raises(PduError):
        decode(_raw(0x44, 0x08, "FF0003", 3))


def test_udl_smaller_than_the_gsm7_header_is_rejected():
    with pytest.raises(PduError):
        decode(_raw(0x44, 0x00, "050003010201", 2))


def test_gsm7_body_shorter_than_udl_is_rejected():
    with pytest.raises(PduError):
        decode(_raw(0x04, 0x00, "C8", 100))


def test_every_truncation_of_a_valid_pdu_is_handled():
    for pdu in (deliver("+14155550123", "Hello world"), deliver("+1555", "验证码", alphabet="ucs2"),
                deliver("+1555", "part", concat=(7, 2, 1))):
        for n in range(0, len(pdu), 2):
            try:
                decode(pdu[:n])
            except PduError:
                pass


def test_fuzz_random_bytes_only_ever_raise_pdu_error():
    rng = random.Random(20261104)
    for _ in range(30000):
        pdu = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 80))).hex()
        try:
            decode(pdu)
        except PduError:
            pass


def test_fuzz_mutated_valid_messages_only_ever_raise_pdu_error():
    rng = random.Random(4242)
    seeds = [bytes.fromhex(p) for p in (
        deliver("+14155550123", "Code 123456 [ok] €"),
        deliver("+8613800138000", "验证码 8888", alphabet="ucs2"),
        deliver("+1555", "long message part", concat=(9, 3, 2)),
    )]
    for _ in range(30000):
        data = bytearray(rng.choice(seeds))
        for _ in range(rng.randrange(1, 5)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        try:
            decode(bytes(data).hex())
        except PduError:
            pass
