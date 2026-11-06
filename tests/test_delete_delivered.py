"""DELETE_DELIVERED: the only feature that can destroy data, so its limits are tested hard."""
import sqlite3
import time

import pytest

from smsgw import atguard
from smsgw.atguard import ForbiddenCommand
from smsgw.chat import ChatClient
from smsgw.main import Channel, Config, Gateway, parse_cmgl_entries
from smsgw.pdu import decode
from smsgw.store import Store
from smsgw.telegram import TelegramClient
from smsgw.usbat import AtChannel
from tests.fakes import FakeChat, FakeModem, FakeTelegram
from tests.pdu_encode import deliver, scts

POISON_PDU = "00" + "44" + "04812143" + "00" + "08" + scts() + "03" + "020003"


class Env:
    def __init__(self, tmp_path):
        self.chat, self.tg, self.modem = FakeChat(), FakeTelegram(), FakeModem()
        self.db_path = str(tmp_path / "s.db")
        self.store = Store(self.db_path)
        self.health = str(tmp_path / "h.json")

    def gateway(self, delete=True, telegram=True, **kw):
        cfg = Config(db_path=self.db_path, startup_notice=False, send_interval=0, health_file=self.health,
                     delete_delivered=delete, **kw)
        tc = TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=self.tg.url) if telegram else None
        return Gateway(cfg, self.store, ChatClient(self.chat.url), lambda: self.modem, tc)

    def deletes(self):
        return [c for c in self.modem.sent if c.startswith("AT+CMGD")]

    def on_modem(self, storage="ME"):
        return [p for p in self.modem.storages[storage] if p is not None]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.chat.close()
    e.tg.close()
    e.store.close()


# ---- the allow-list -------------------------------------------------------
def test_delete_is_forbidden_by_default():
    with pytest.raises(ForbiddenCommand):
        atguard.check("AT+CMGD=1")


@pytest.mark.parametrize("cmd", [
    "AT+CMGD=1,4", "AT+CMGD=1,1", "AT+CMGD=0,3", "AT+CMGD=", "AT+CMGD", "AT+CMGD=-1", "AT+CMGD=99999",
    "AT+CMGD=1;+CMGD=2", "AT+CMGD=1\rAT+CMGD=2", "at+cmgd=1", "AT+CMGD=1 ,4", "AT+CMGD=abc",
])
def test_only_a_single_index_is_ever_allowed(cmd):
    with pytest.raises(ForbiddenCommand):
        atguard.check(cmd, allow_delete=True)


def test_single_index_is_allowed_only_when_enabled():
    assert atguard.check("AT+CMGD=12", allow_delete=True) == "AT+CMGD=12"


@pytest.mark.parametrize("cmd", ['AT+CMGS="+1555"', "AT+CMGW", "ATD123;", "AT+CFUN=0", 'AT+QCFG="usbnet",1'])
def test_enabling_delete_does_not_open_anything_else(cmd):
    with pytest.raises(ForbiddenCommand):
        atguard.check(cmd, allow_delete=True)


def test_channel_refuses_delete_before_writing_when_disabled():
    m = FakeModem()
    with pytest.raises(ForbiddenCommand):
        AtChannel(m).command("AT+CMGD=1")
    assert m.sent == []


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False), ("true", False), ("", False)])
def test_only_exactly_1_enables_the_switch(monkeypatch, value, expected):
    monkeypatch.setenv("DELETE_DELIVERED", value)
    assert Config.from_env().delete_delivered is expected


def test_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv("DELETE_DELIVERED", raising=False)
    assert Config.from_env().delete_delivered is False


def test_parse_cmgl_entries_keeps_indexes():
    assert parse_cmgl_entries(["+CMGL: 7,1,,20", "0791AB", "+CMGL: x", "0791CD", "+CMGL: 9,1,,5", "bad!"]) == [
        (7, "0791AB"), (None, "0791CD")]


# ---- when it may delete ---------------------------------------------------
def test_switch_off_never_deletes(env):
    gw = env.gateway(delete=False)
    env.modem.add("ME", deliver("+1555", "hello"))
    for t in range(3):
        gw.poll_once(now=t)
        gw.flush_outbox(now=t)
    assert env.deletes() == [] and len(env.on_modem()) == 1


def test_deleted_only_after_every_channel_confirmed(env):
    gw = env.gateway()
    env.modem.add("ME", deliver("+1555", "hello"))
    env.tg.fail = 1000
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    assert len(env.chat.messages) == 1 and env.tg.messages == []
    gw.poll_once(now=1)
    assert env.deletes() == [] and len(env.on_modem()) == 1     # Telegram still missing
    env.tg.fail = 0
    gw.flush_outbox(now=31)
    gw.poll_once(now=32)
    assert env.deletes() == ["AT+CMGD=0"] and env.on_modem() == []
    gw.poll_once(now=33)
    assert env.deletes() == ["AT+CMGD=0"]
    assert len(env.chat.messages) == 1 and len(env.tg.messages) == 1


def test_nothing_is_deleted_during_a_total_outage(env):
    gw = env.gateway()
    env.modem.add("ME", deliver("+1555", "stuck"))
    env.chat.fail = env.tg.fail = 1000
    for t in range(0, 1000, 50):
        gw.poll_once(now=t)
        gw.flush_outbox(now=t)
    assert env.deletes() == [] and len(env.on_modem()) == 1
    assert env.store.pending_count() == 1


def test_refused_message_is_not_deleted(env):
    gw = env.gateway(telegram=False)
    env.modem.add("ME", deliver("+1555", "refused"))
    env.chat.reject_next = 1
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    gw.poll_once(now=1)
    assert env.deletes() == []


def test_copy_stays_in_the_database_after_deletion(env):
    gw = env.gateway(telegram=False)
    env.modem.add("ME", deliver("+1555", "keep a copy"))
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    gw.poll_once(now=1)
    assert env.on_modem() == []
    assert env.store.db.execute("SELECT text FROM outbox").fetchone()[0] == "keep a copy"


def test_long_message_parts_wait_until_it_is_complete_and_delivered(env):
    gw = env.gateway(telegram=False)
    env.modem.add("ME", deliver("+1555", "part one ", concat=(9, 2, 1)))
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    gw.poll_once(now=1)
    assert env.deletes() == []
    env.modem.add("ME", deliver("+1555", "part two", concat=(9, 2, 2)))
    gw.poll_once(now=2)
    assert env.deletes() == []                       # complete, but not delivered yet
    gw.flush_outbox(now=2)
    gw.poll_once(now=3)
    assert sorted(env.deletes()) == ["AT+CMGD=0", "AT+CMGD=1"] and env.on_modem() == []
    assert "part one part two" in env.chat.messages[0]


def test_undecodable_message_is_forwarded_but_never_deleted(env):
    gw = env.gateway(telegram=False)
    env.modem.add("ME", POISON_PDU)
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    assert "无法解码" in env.chat.messages[0]
    gw.poll_once(now=1)
    assert env.deletes() == [] and env.on_modem() == [POISON_PDU]


def test_first_run_skip_never_deletes_what_was_not_forwarded(env):
    gw = env.gateway(telegram=False, first_run_backlog="skip")
    env.modem.add("ME", deliver("+1555", "old, not announced"))
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    gw.poll_once(now=1)
    assert env.chat.messages == [] and env.deletes() == []


def test_status_reports_stay(env):
    gw = env.gateway(telegram=False)
    env.modem.add("ME", "00" + "02" + "00" * 20)
    gw.poll_once(now=0)
    gw.poll_once(now=1)
    assert env.deletes() == []


def test_message_unknown_to_the_database_is_not_deletable(env):
    assert env.store.can_delete(deliver("+1555", "never seen")) is False


def test_message_delivered_and_pruned_is_deletable(env):
    pdu = deliver("+1555", "old")
    env.store.add(pdu, decode(pdu), now=0)
    env.store.mark_channel(env.store.due(now=0)[0].id, 1, 1, now=0)
    env.store.prune(14, now=100 * 86400)
    assert env.store.can_delete(pdu) is True


# ---- messages stored by versions before 0.2.2 ------------------------------
def legacy_row(env, pdu, sender="+1555"):
    env.store.db.execute(
        "INSERT INTO raw_parts(hash,sender,ts,text,seen_at,composed) VALUES(?,?,'','x',0,1)",
        (env.store.pdu_hash(pdu), sender))
    env.store.db.commit()


def test_legacy_message_is_deletable_when_nothing_from_that_sender_waits(env):
    pdu = deliver("+1555", "legacy")
    legacy_row(env, pdu)
    assert env.store.can_delete(pdu) is True


def test_legacy_message_stays_while_that_sender_has_something_waiting(env):
    pdu = deliver("+1555", "legacy")
    legacy_row(env, pdu)
    other = deliver("+1555", "waiting")
    env.store.add(other, decode(other), now=0)
    assert env.store.can_delete(pdu) is False


def test_full_storage_clears_and_new_sms_arrives(env):
    """23/23 used, everything already delivered by the old version, new SMS bounce until there is room."""
    env.modem.capacity = 23
    for i in range(23):
        pdu = deliver("+1555", f"old {i}")
        assert env.modem.add("ME", pdu) is True
        legacy_row(env, pdu)
    assert env.modem.add("ME", deliver("+1555", "bounced")) is False

    gw = env.gateway()
    gw.poll_once(now=0)
    assert gw.storage_usage["ME"] == (0, 23)
    assert len(env.deletes()) == 23 and env.on_modem() == []
    assert env.chat.messages == [] and env.tg.messages == []    # old ones are not announced again

    assert env.modem.add("ME", deliver("+1555", "new one")) is True
    gw.poll_once(now=1)
    gw.flush_outbox(now=1)
    assert "new one" in env.chat.messages[0] and "new one" in env.tg.messages[0]
    gw.poll_once(now=2)
    assert env.on_modem() == []


def test_storage_alert_clears_after_cleaning(env):
    env.modem.capacity = 2
    for i in range(2):
        pdu = deliver("+1555", f"old {i}")
        env.modem.add("ME", pdu)
        legacy_row(env, pdu)
    gw = env.gateway(delete=False, telegram=False)
    gw.poll_once(now=0)
    gw._supervise_storage(0)
    assert any("快满了" in m for m in env.chat.messages)

    gw = env.gateway(telegram=False)
    gw.poll_once(now=1)
    gw._supervise_storage(1)
    assert any("✅" in m for m in env.chat.messages)


# ---- failure handling -----------------------------------------------------
def test_delete_error_keeps_the_message_and_retries_next_poll(env):
    gw = env.gateway(telegram=False)
    env.modem.add("ME", deliver("+1555", "a"))
    env.modem.add("ME", deliver("+1555", "b"))
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    env.modem.delete_error = True
    gw.poll_once(now=1)                              # must not raise
    assert len(env.deletes()) == 1 and len(env.on_modem()) == 2  # stops after the first error
    env.modem.delete_error = False
    gw.poll_once(now=2)
    assert env.on_modem() == []


def test_indexes_are_stable_with_gaps(env):
    gw = env.gateway(telegram=False)
    for name in ("a", "b", "c"):
        env.modem.add("ME", deliver("+1555", name))
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    env.modem.delete(0, "ME")                        # slot 0 emptied by something else
    gw.poll_once(now=1)
    assert sorted(env.deletes()) == ["AT+CMGD=1", "AT+CMGD=2"]  # index 0 was already gone


def test_second_storage_is_cleaned_too(env):
    gw = env.gateway(telegram=False)
    env.modem.add("SM", deliver("+1555", "on the SIM"))
    gw.poll_once(now=0)
    gw.flush_outbox(now=0)
    gw.poll_once(now=1)
    assert env.on_modem("SM") == []


@pytest.mark.integration
def test_full_loop_with_the_switch_on_a_full_modem(tmp_path, monkeypatch):
    """The real loop: storage full of delivered SMS, the switch on, then a new SMS gets through."""
    monkeypatch.setattr(Channel, "RETRY_BASE", 0.05)
    env = Env(tmp_path)
    env.modem.capacity = 5
    for i in range(5):
        pdu = deliver("+1555", f"old {i}")
        env.modem.add("ME", pdu)
        legacy_row(env, pdu)
    env.store.close()
    cfg = Config(db_path=env.db_path, startup_notice=False, send_interval=0, health_file=env.health,
                 delete_delivered=True, poll_interval=0.02)

    import threading
    holder = {}

    def runner():
        store = Store(env.db_path)
        tc = TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=env.tg.url)
        holder["gw"] = Gateway(cfg, store, ChatClient(env.chat.url), lambda: env.modem, tc)
        holder["gw"].run()
        store.close()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and env.on_modem():
            time.sleep(0.02)
        assert env.on_modem() == []
        assert env.modem.add("ME", deliver("+1555", "fresh")) is True
        while time.monotonic() < deadline and not any("fresh" in m for m in env.tg.messages):
            time.sleep(0.02)
        assert any("fresh" in m for m in env.chat.messages) and any("fresh" in m for m in env.tg.messages)
        assert not any("old " in m for m in env.chat.messages)
        while time.monotonic() < deadline and env.on_modem():
            time.sleep(0.02)
        assert env.on_modem() == []
    finally:
        holder["gw"].stop()
        t.join(20)
        env.chat.close()
        env.tg.close()
    db = sqlite3.connect(env.db_path)
    assert db.execute("SELECT COUNT(*) FROM outbox WHERE text='fresh' AND sent_at IS NOT NULL").fetchone()[0] == 1
    db.close()
