"""Regression tests for bugs in 0.2.0 (each one failed on that version)."""
import socket
import threading
import time

import pytest

from smsgw.chat import ChatClient, ChatError
from smsgw.main import Config, Gateway
from smsgw.pdu import PduError, decode
from smsgw.store import Store
from smsgw.usbat import UsbError
from tests.fakes import FakeChat, FakeModem
from tests.pdu_encode import deliver, scts

# SMS-DELIVER with UDHI set whose UDH claims a concat element longer than the data.
POISON_PDU = "00" + "44" + "04812143" + "00" + "08" + scts() + "03" + "020003"


def make_gateway(tmp_path, srv, modem=None, **kw):
    cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False, **kw)
    store = Store(cfg.db_path)
    modem = modem or FakeModem()
    return Gateway(cfg, store, ChatClient(srv.url), lambda: modem), modem, store


def test_malformed_udh_raises_pdu_error_not_index_error():
    with pytest.raises(PduError):
        decode(POISON_PDU)


def test_poison_message_does_not_kill_polling_and_user_is_told(tmp_path):
    srv = FakeChat()
    try:
        gw, modem, store = make_gateway(tmp_path, srv)
        modem.add("ME", POISON_PDU)
        modem.add("ME", deliver("+1555", "after the poison"))
        gw.poll_once(now=0)
        gw.flush_outbox(now=0)
        assert any("无法解码" in m for m in srv.messages)
        assert any("after the poison" in m for m in srv.messages)
    finally:
        srv.close()


def test_duplicate_part_does_not_cause_false_incomplete_alert(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    a1 = decode(deliver("+1555", "Hello ", concat=(9, 2, 1)))
    dup = decode(deliver("+1555", "Hello!", concat=(9, 2, 1)))     # same seq, different bytes
    a2 = decode(deliver("+1555", "world", concat=(9, 2, 2)))
    store.add(deliver("+1555", "Hello ", concat=(9, 2, 1)), a1, now=0)
    store.add(deliver("+1555", "Hello!", concat=(9, 2, 1)), dup, now=0)
    store.add(deliver("+1555", "world", concat=(9, 2, 2)), a2, now=0)
    assert store.pending_count() == 1
    assert store.flush_incomplete(120, now=1000) == 0
    assert store.pending_count() == 1


def test_first_run_skip_also_applies_to_incomplete_long_sms(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    part = deliver("+1555", "old part", concat=(3, 2, 1))
    store.add(part, decode(part), notify=False, now=0)
    assert store.flush_incomplete(120, now=1000) == 0 or store.pending_count() == 0
    assert store.pending_count() == 0


def test_message_still_on_sim_is_not_reannounced_after_400_days(tmp_path):
    srv = FakeChat()
    try:
        gw, modem, store = make_gateway(tmp_path, srv)
        modem.add("ME", deliver("+1555", "old but still on the SIM"))
        day = 86400
        gw.poll_once(now=0)
        gw.flush_outbox(now=0)
        assert len(srv.messages) == 1
        for d in (100, 200, 300, 395):               # still present in every poll
            gw.poll_once(now=d * day)
            store.prune(14, now=d * day)
        store.prune(14, now=401 * day)
        gw.poll_once(now=401 * day)
        gw.flush_outbox(now=401 * day)
        assert len(srv.messages) == 1
    finally:
        srv.close()


def test_garbage_http_response_is_a_chat_error_not_a_crash():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall(b"this is not http\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    c = ChatClient(f"http://127.0.0.1:{port}/webapi?api=SYNO.Chat.External&token=SECRET")
    try:
        with pytest.raises(ChatError):
            c.post("x")
    finally:
        srv.close()


def test_backlog_is_still_delivered_while_modem_is_down(tmp_path):
    srv = FakeChat()
    try:
        cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False, poll_interval=0.05)
        store = Store(cfg.db_path)
        pdu = deliver("+1555", "stored before the dongle died")
        store.add(pdu, decode(pdu))
        store.db.close()

        def broken_modem():
            raise UsbError("device gone")

        result = {}

        def runner():
            s = Store(cfg.db_path)                   # SQLite connections are per-thread
            gw = Gateway(cfg, s, ChatClient(srv.url), broken_modem)
            result["gw"] = gw
            gw.run()
            s.close()

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not srv.messages:
            time.sleep(0.05)
        result["gw"].stop()
        t.join(10)
        assert any("stored before the dongle died" in m for m in srv.messages)
    finally:
        srv.close()


def test_long_outage_does_not_delay_delivery_by_30_minutes_after_recovery(tmp_path):
    srv = FakeChat(fail=12)
    try:
        gw, modem, store = make_gateway(tmp_path, srv)
        modem.add("ME", deliver("+1555", "stuck during the outage"))
        gw.poll_once(now=0)
        t = 0
        while srv.fail > 0 and t < 86400:            # retry for as long as the server fails
            gw.flush_outbox(now=t)
            t += 10
        assert srv.fail == 0
        assert store.pending_count() == 1
        # the server is healthy again: delivery must happen within 5 minutes
        assert gw.flush_outbox(now=t + 301) == 1
        assert len(srv.messages) == 1
    finally:
        srv.close()
