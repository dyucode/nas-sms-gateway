import pytest

from smsgw.chat import ChatClient
from smsgw.main import Config, Gateway
from smsgw.store import Store
from smsgw.telegram import TelegramClient, TelegramError, find_chat_ids, parse_credentials
from tests.fakes import FakeChat, FakeModem, FakeTelegram
from tests.pdu_encode import deliver


def make(tmp_path, chat, tg):
    cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False)
    store = Store(cfg.db_path)
    modem = FakeModem()
    tc = TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=tg.url)
    return Gateway(cfg, store, ChatClient(chat.url), lambda: modem, tc), modem, store


def test_sms_goes_to_both_channels(tmp_path):
    chat, tg = FakeChat(), FakeTelegram()
    try:
        gw, modem, store = make(tmp_path, chat, tg)
        modem.add("ME", deliver("+15551230010", "code 123456"))
        gw.poll_once(now=0)
        assert gw.flush_outbox(now=0) == 1
        assert len(chat.messages) == 1 and len(tg.messages) == 1
        assert "0010" in tg.messages[0] and "123456" in tg.messages[0]
        assert tg.bodies[0]["chat_id"] == FakeTelegram.CHAT
        assert tg.paths[0] == f"/bot{FakeTelegram.TOKEN}/sendMessage"
        assert store.pending_count() == 0
        assert gw.flush_outbox(now=1000) == 0 and len(tg.messages) == 1
    finally:
        chat.close(); tg.close()


def test_telegram_failure_retries_only_telegram(tmp_path):
    chat, tg = FakeChat(), FakeTelegram(fail=1)
    try:
        gw, modem, store = make(tmp_path, chat, tg)
        modem.add("ME", deliver("+1555", "hello"))
        gw.poll_once(now=0)
        assert gw.flush_outbox(now=0) == 0
        assert len(chat.messages) == 1 and len(tg.messages) == 0 and store.pending_count() == 1
        assert gw.flush_outbox(now=31) == 1
        assert len(chat.messages) == 1 and len(tg.messages) == 1 and store.pending_count() == 0
    finally:
        chat.close(); tg.close()


def test_chat_failure_retries_only_chat(tmp_path):
    chat, tg = FakeChat(fail=1), FakeTelegram()
    try:
        gw, modem, store = make(tmp_path, chat, tg)
        modem.add("ME", deliver("+1555", "hello"))
        gw.poll_once(now=0)
        assert gw.flush_outbox(now=0) == 0
        assert len(tg.messages) == 1 and len(chat.messages) == 0
        assert gw.flush_outbox(now=31) == 1
        assert len(tg.messages) == 1 and len(chat.messages) == 1
    finally:
        chat.close(); tg.close()


def test_works_without_telegram(tmp_path):
    chat = FakeChat()
    try:
        cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False)
        store = Store(cfg.db_path); modem = FakeModem()
        gw = Gateway(cfg, store, ChatClient(chat.url), lambda: modem)
        modem.add("ME", deliver("+1555", "hi"))
        gw.poll_once(now=0)
        assert gw.flush_outbox(now=0) == 1 and store.pending_count() == 0
    finally:
        chat.close()


def test_rejected_and_errors_hide_secrets():
    tg = FakeTelegram(reject=True)
    try:
        c = TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=tg.url)
        with pytest.raises(TelegramError) as ei:
            c.post("secret body")
        assert FakeTelegram.TOKEN not in str(ei.value) and "secret body" not in str(ei.value)
    finally:
        tg.close()
    tg = FakeTelegram(fail=1)
    try:
        with pytest.raises(TelegramError) as ei:
            TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=tg.url).post("x")
        assert FakeTelegram.TOKEN not in str(ei.value)
    finally:
        tg.close()
    c = TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api="http://127.0.0.1:9")
    with pytest.raises(TelegramError) as ei:
        c.post("x")
    assert FakeTelegram.TOKEN not in str(ei.value)


def test_credentials_parsing():
    tok = FakeTelegram.TOKEN
    assert parse_credentials(f"{tok}\r\n-100123\n") == (tok, "-100123")
    for bad in ("", tok, f"{tok}\nabc", f"{tok}\n1\n2", "badtoken\n1", f"1\n{tok}"):
        with pytest.raises(TelegramError):
            parse_credentials(bad)


def test_long_text_truncated():
    tg = FakeTelegram()
    try:
        TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=tg.url).post("x" * 9000)
        assert len(tg.bodies[0]["text"]) == 4096
    finally:
        tg.close()


def test_find_chat_ids_token_only_file(tmp_path):
    tg = FakeTelegram(updates=[{"message": {"chat": {"id": 777, "type": "private", "first_name": "Dan"}}}])
    try:
        f = tmp_path / "t.txt"; f.write_text(FakeTelegram.TOKEN + "\n")
        assert find_chat_ids(str(f), api=tg.url) == ["777 (private, Dan)"]
    finally:
        tg.close()
