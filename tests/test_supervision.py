"""Unit tests for channel failure handling, alerts, health and the heartbeat."""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from smsgw.chat import ChatClient, ChatError
from smsgw.main import Channel, Config, Gateway, check_health
from smsgw.pdu import decode
from smsgw.store import Store
from smsgw.telegram import TelegramClient, TelegramError
from smsgw.usbat import UsbError
from tests.fakes import FakeChat, FakeModem, FakeTelegram
from tests.pdu_encode import deliver


class Env:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.chat = FakeChat()
        self.tg = FakeTelegram()
        self.modem = FakeModem()
        self.db_path = str(tmp_path / "s.db")
        self.store = Store(self.db_path)

    def gateway(self, telegram=True, factory=None, store=None, **kw):
        kw.setdefault("startup_notice", False)
        kw.setdefault("send_interval", 0)
        kw.setdefault("health_file", str(self.tmp_path / "health.json"))
        cfg = Config(db_path=self.db_path, **kw)
        tc = TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=self.tg.url) if telegram else None
        return Gateway(cfg, store or self.store, ChatClient(self.chat.url), factory or (lambda: self.modem), tc)

    def add(self, text, now=0.0, sender="+1555"):
        pdu = deliver(sender, text)
        self.store.add(pdu, decode(pdu), now=now)

    def alerts(self, srv):
        return [m for m in srv.messages if "⚠️" in m or "✅" in m]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.chat.close()
    e.tg.close()
    e.store.close()


# ---- channel backoff ------------------------------------------------------
def test_channel_backoff_grows_is_capped_and_resets():
    ch = Channel("x", 1, None, None)
    err = ChatError("down")
    delays = []
    for _ in range(8):
        ch.failed(0, err)
        delays.append(ch.retry_at)
    assert delays == [30, 60, 120, 240, 300, 300, 300, 300]
    assert not ch.available(299) and ch.available(300)
    ch.succeeded()
    assert ch.available(0) and ch.failures == 0 and ch.last_error == ""


def test_refusal_does_not_put_the_channel_in_backoff():
    ch = Channel("x", 1, None, None)
    ch.failed(0, ChatError("refused", transient=False))
    assert ch.available(0) and ch.last_error == "refused"


def test_outage_probes_one_message_and_recovery_sends_all(env):
    gw = env.gateway(telegram=False)
    for i in range(5):
        env.add(f"msg{i}")
    env.chat.fail = 1000
    assert gw.flush_outbox(now=0) == 0
    assert env.chat.requests == 1                    # no hammering a dead channel
    assert gw.flush_outbox(now=29) == 0 and env.chat.requests == 1
    env.chat.fail = 0
    assert gw.flush_outbox(now=30) == 5
    assert [m.split("\n")[-1] for m in env.chat.messages] == [f"msg{i}" for i in range(5)]


def test_refused_message_does_not_block_the_next_round(env):
    gw = env.gateway(telegram=False)
    env.add("refused once")
    env.add("next")
    env.chat.reject_next = 1
    assert gw.flush_outbox(now=0) == 0
    assert env.chat.requests == 1                    # may be rate limiting: stop for this round
    assert gw.flush_outbox(now=1) == 1               # the refused one waits, the next one goes
    assert "next" in env.chat.messages[0]
    assert gw.flush_outbox(now=31) == 1
    assert "refused once" in env.chat.messages[1]


def test_one_channel_down_does_not_stop_the_other(env):
    gw = env.gateway()
    env.add("a")
    env.add("b")
    env.chat.fail = 1000
    assert gw.flush_outbox(now=0) == 0
    assert len(env.tg.messages) == 2 and env.chat.messages == []
    assert env.store.pending_count() == 2
    env.chat.fail = 0
    assert gw.flush_outbox(now=30) == 2
    assert len(env.tg.messages) == 2 and len(env.chat.messages) == 2


def test_backlog_is_paced_per_channel(env, monkeypatch):
    gw = env.gateway(telegram=False, send_interval=5.0)
    naps = []
    monkeypatch.setattr(gw, "_sleep", naps.append)
    env.add("a")
    env.add("b")
    gw.flush_outbox(now=0)
    assert len(naps) == 1 and 0 < naps[0] <= 5.0


def test_stop_interrupts_a_backlog(env):
    gw = env.gateway(telegram=False)
    env.add("a")
    gw.stop()
    assert gw.flush_outbox(now=0) == 0
    assert env.chat.requests == 0 and env.store.pending_count() == 1


def test_removed_telegram_channel_closes_finished_messages(env):
    env.add("a")
    env.store.mark_channel(env.store.due(now=0)[0].id, 1, 3, now=0)
    gw = env.gateway(telegram=False)
    gw.flush_outbox(now=0)
    assert env.store.pending_count() == 0 and env.chat.requests == 0


# ---- error classification -------------------------------------------------
def test_chat_error_classes(env):
    c = ChatClient(env.chat.url)
    env.chat.fail = 1
    with pytest.raises(ChatError) as e:
        c.post("x")
    assert e.value.transient and "500" in str(e.value)
    env.chat.reject_next = 1
    with pytest.raises(ChatError) as e:
        c.post("x")
    assert not e.value.transient and "code 117" in str(e.value)


@pytest.mark.parametrize("status,transient", [(400, False), (401, True), (403, True), (429, True), (500, True)])
def test_telegram_error_classes(env, status, transient):
    env.tg.fail, env.tg.status = 1, status
    with pytest.raises(TelegramError) as e:
        TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=env.tg.url).post("x")
    assert e.value.transient is transient


# ---- delivery alert -------------------------------------------------------
def test_delivery_alert_goes_through_the_working_channel_once(env):
    gw = env.gateway()
    env.add("stuck", now=0)
    env.chat.fail = 1000
    gw.flush_outbox(now=0)
    gw._supervise_delivery(899)
    assert env.alerts(env.tg) == []
    gw._supervise_delivery(900)
    alerts = env.alerts(env.tg)
    assert len(alerts) == 1 and "1 条短信" in alerts[0] and "Chat" in alerts[0] and "500" in alerts[0]
    gw._supervise_delivery(1000)
    assert len(env.alerts(env.tg)) == 1


def test_delivery_alert_never_contains_sms_content(env):
    gw = env.gateway()
    env.add("TOPSECRET", now=0, sender="+14155550123")
    env.chat.fail = 1000
    gw.flush_outbox(now=0)
    gw._supervise_delivery(1000)
    alert = env.alerts(env.tg)[0]
    assert "TOPSECRET" not in alert and "4155550123" not in alert


def test_delivery_alert_repeats_daily_then_clears_once(env):
    gw = env.gateway()
    env.add("stuck", now=0)
    env.chat.fail = 1000
    gw.flush_outbox(now=0)
    gw._supervise_delivery(900)
    gw._supervise_delivery(900 + 86399)
    assert len(env.alerts(env.tg)) == 1
    gw._supervise_delivery(900 + 86400)
    assert len(env.alerts(env.tg)) == 2
    env.chat.fail = 0
    recovered = 900 + 86400 + 301                    # Chat's probe pause after the failed alert is over
    gw.flush_outbox(now=recovered)
    assert env.store.pending_count() == 0
    gw._supervise_delivery(recovered)
    gw._supervise_delivery(recovered + 100)
    assert sum("✅" in m for m in env.tg.messages) == 1
    assert sum("✅" in m for m in env.chat.messages) == 1


def test_alert_waits_until_some_channel_can_carry_it(env):
    gw = env.gateway()
    env.add("stuck", now=0)
    env.chat.fail = env.tg.fail = 1000
    gw._supervise_delivery(1000)
    assert env.store.get_meta("delivery_alert_at") is None
    env.chat.fail = env.tg.fail = 0
    gw._supervise_delivery(1000 + 300)
    assert len(env.alerts(env.tg)) == 1 and env.store.get_meta("delivery_alert_at") is not None


def test_total_outage_is_explained_after_recovery_even_though_no_alert_could_be_sent(env):
    gw = env.gateway()
    env.add("stuck", now=0)
    env.chat.fail = env.tg.fail = 1000
    gw.flush_outbox(now=0)
    gw._supervise_delivery(1000)
    assert env.alerts(env.tg) == [] and env.alerts(env.chat) == []
    env.chat.fail = env.tg.fail = 0
    gw.flush_outbox(now=1400)
    assert env.store.pending_count() == 0
    gw._supervise_delivery(1400)
    assert sum("✅" in m for m in env.tg.messages) == 1
    gw._supervise_delivery(1500)
    assert sum("✅" in m for m in env.tg.messages) == 1  # only once


def test_alert_is_not_repeated_after_a_restart(env):
    env.add("stuck", now=0)
    env.chat.fail = 1000
    gw = env.gateway()
    gw.flush_outbox(now=0)
    gw._supervise_delivery(900)
    gw2 = env.gateway()
    gw2._supervise_delivery(1000)
    assert len(env.alerts(env.tg)) == 1


def test_no_alert_for_a_message_that_is_just_on_its_way(env):
    gw = env.gateway()
    env.add("fresh", now=1000)
    gw._supervise_delivery(1001)
    assert env.alerts(env.chat) == [] and env.alerts(env.tg) == []


# ---- modem alert ----------------------------------------------------------
def test_modem_alert_is_retried_when_nothing_could_carry_it(env):
    gw = env.gateway(telegram=False, alert_after=0.0)
    env.chat.fail = 1
    gw._note_problem(0.0, "UsbError")
    assert not gw._alerted
    gw._note_problem(100.0, "UsbError")
    assert gw._alerted and len(env.alerts(env.chat)) == 1


# ---- storage alert --------------------------------------------------------
def fill(modem, storage, n):
    for i in range(n):
        modem.add(storage, deliver("+1555", f"sms {storage} {i}"))


def test_storage_alert_at_threshold_and_clear(env):
    gw = env.gateway(telegram=False)
    fill(env.modem, "ME", 39)
    gw.poll_once(now=0)
    gw._supervise_storage(0)
    assert gw.storage_usage["ME"] == (39, 50)
    assert env.alerts(env.chat) == []
    fill(env.modem, "SM", 40)
    gw.poll_once(now=600)
    gw._supervise_storage(600)
    alerts = env.alerts(env.chat)
    assert len(alerts) == 1 and "SM 已用 40/50" in alerts[0] and "ME" not in alerts[0]
    gw._supervise_storage(700)
    assert len(env.alerts(env.chat)) == 1
    env.modem.storages["SM"] = []
    gw.poll_once(now=1200)
    gw._supervise_storage(1200)
    assert any("✅" in m for m in env.chat.messages)


def test_storage_is_checked_only_periodically(env):
    gw = env.gateway(telegram=False)
    gw.poll_once(now=0)
    gw.poll_once(now=599)
    assert env.modem.sent.count("AT+CPMS?") == 2     # ME and SM, once
    gw.poll_once(now=600)
    assert env.modem.sent.count("AT+CPMS?") == 4


def test_modem_without_usage_support_still_receives(env):
    env.modem.usage_supported = False
    env.modem.add("ME", deliver("+1555", "hello"))
    gw = env.gateway(telegram=False)
    assert gw.poll_once(now=0) == 1
    gw._supervise_storage(0)
    assert gw.storage_usage == {} and env.alerts(env.chat) == []


# ---- startup --------------------------------------------------------------
def test_startup_notice_is_rate_limited_across_restarts(env):
    for _ in range(3):
        env.gateway(telegram=False, startup_notice=True)._startup()
    assert sum("已启动" in m for m in env.chat.messages) == 1


def test_startup_notice_is_retried_if_it_could_not_be_sent(env):
    env.chat.fail = 1
    env.gateway(telegram=False, startup_notice=True)._startup()
    assert env.store.get_meta("startup_notice_at") is None
    env.gateway(telegram=False, startup_notice=True)._startup()
    assert sum("已启动" in m for m in env.chat.messages) == 1


def test_startup_clears_per_message_backoff(env):
    env.add("waiting")
    env.store.mark_failed(env.store.due(now=0)[0].id, now=0)
    assert env.store.due(now=1) == []
    env.gateway(telegram=False)._startup()
    assert len(env.store.due(now=1)) == 1


# ---- tick isolation -------------------------------------------------------
def test_each_step_survives_a_failure_in_the_others(env, monkeypatch, caplog):
    gw = env.gateway(telegram=False)

    def boom(*_a, **_k):
        raise RuntimeError("SECRET SMS BODY")

    monkeypatch.setattr(gw, "poll_once", boom)
    monkeypatch.setattr(gw, "_prune", boom)
    env.add("still delivered")
    assert gw.tick(now=0) == 5.0
    assert len(env.chat.messages) == 1
    assert "unexpected RuntimeError during poll" in caplog.text
    assert "unexpected RuntimeError during prune" in caplog.text
    assert "SECRET SMS BODY" not in caplog.text


def test_modem_retry_delay_grows_and_resets(env):
    state = {"broken": True}

    def factory():
        if state["broken"]:
            raise UsbError("gone")
        return env.modem

    gw = env.gateway(telegram=False, factory=factory)
    assert [gw.tick(now=0) for _ in range(6)] == [5, 10, 20, 40, 60, 60]
    state["broken"] = False
    assert gw.tick(now=0) == 10.0
    state["broken"] = True
    gw._close()
    assert gw.tick(now=0) == 5.0


def test_sim_not_ready_keeps_polling_and_delivers(env):
    env.modem.sim = "SIM PIN"
    gw = env.gateway(telegram=False)
    env.add("stored")
    assert gw.tick(now=0) == 10.0
    assert len(env.chat.messages) == 1


def test_prune_runs_hourly(env, monkeypatch):
    gw = env.gateway(telegram=False)
    calls = []
    monkeypatch.setattr(env.store, "prune", lambda *a, **k: calls.append(k["now"]))
    for t in (4000, 4100, 7601):
        gw._prune(t)
    assert calls == [4000, 7601]


# ---- health ---------------------------------------------------------------
def health_status(env, gw, now):
    gw._write_health(now)
    return check_health(gw.cfg, now=now)


def test_health_ok_and_written_atomically(env):
    gw = env.gateway(telegram=False)
    gw.tick(now=100)
    code, text = check_health(gw.cfg, now=101)
    assert code == 0 and "ok" in text
    status = json.load(open(gw.cfg.health_file, encoding="utf-8"))
    assert status["modem_ok"] is True and status["storage"]["ME"] == [0, 50]


def test_health_fails_without_data_or_with_bad_data(env):
    gw = env.gateway(telegram=False)
    assert check_health(gw.cfg, now=0)[0] == 1
    (env.tmp_path / "health.json").write_text("not json")
    assert check_health(gw.cfg, now=0)[0] == 1
    (env.tmp_path / "health.json").write_text("{}")
    assert check_health(gw.cfg, now=0)[0] == 1


def test_health_fails_when_the_loop_stalls(env):
    gw = env.gateway(telegram=False)
    gw._write_health(0)
    assert check_health(gw.cfg, now=180)[0] == 0
    assert check_health(gw.cfg, now=181)[0] == 1


def test_health_fails_when_delivery_is_overdue(env):
    gw = env.gateway(telegram=False)
    env.add("stuck", now=0)
    code, text = health_status(env, gw, 900)
    assert code == 1 and "1 SMS" in text


def test_health_fails_when_the_modem_stays_down(env):
    gw = env.gateway(telegram=False, alert_after=300.0)
    gw._note_problem(0.0, "UsbError")
    assert health_status(env, gw, 299)[0] == 0
    assert health_status(env, gw, 300)[0] == 1


def test_health_write_failure_does_not_stop_the_gateway(env, caplog):
    gw = env.gateway(telegram=False, health_file=str(env.tmp_path / "no" / "such" / "dir" / "h.json"))
    env.add("stored")
    gw.tick(now=0)
    assert len(env.chat.messages) == 1
    assert "during health" in caplog.text


# ---- heartbeat ------------------------------------------------------------
class Pings:
    def __init__(self, status=200):
        self.count = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.count += 1
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"OK")

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/ping/SECRETUUID"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_heartbeat_only_while_everything_works(env):
    pings = Pings()
    try:
        gw = env.gateway(telegram=False, heartbeat_url=pings.url)
        gw._modem_ok = True
        gw._heartbeat(1000)
        gw._heartbeat(1299)
        assert pings.count == 1
        gw._heartbeat(1300)
        assert pings.count == 2
        gw._modem_ok = False
        gw._heartbeat(5000)
        assert pings.count == 2
        gw._modem_ok = True
        env.add("stuck", now=0)
        gw._heartbeat(6000)
        assert pings.count == 2
    finally:
        pings.close()


def test_heartbeat_failure_is_harmless_and_hides_the_url(env, caplog):
    caplog.set_level(logging.DEBUG)
    pings = Pings(status=500)
    try:
        gw = env.gateway(telegram=False, heartbeat_url=pings.url)
        gw._modem_ok = True
        gw._heartbeat(1000)
        assert "heartbeat failed" in caplog.text and "SECRETUUID" not in caplog.text
    finally:
        pings.close()


def test_no_heartbeat_when_not_configured(env):
    gw = env.gateway(telegram=False)
    gw._modem_ok = True
    gw._heartbeat(1000)
    assert gw._last_heartbeat == 0.0


# ---- configuration --------------------------------------------------------
def test_config_from_env(monkeypatch):
    for k, v in {"POLL_INTERVAL": "3", "STORAGES": "sm, me", "DELIVERY_ALERT_AFTER": "60", "SEND_INTERVAL": "0.5",
                 "STORAGE_ALERT_PCT": "90", "HEARTBEAT_URL": "http://x", "STARTUP_NOTICE": "0",
                 "USB_VID": "1234", "CHAT_INSECURE_TLS": "1"}.items():
        monkeypatch.setenv(k, v)
    cfg = Config.from_env()
    assert cfg.poll_interval == 3 and cfg.storages == ("SM", "ME") and cfg.delivery_alert_after == 60
    assert cfg.send_interval == 0.5 and cfg.storage_alert_pct == 90 and cfg.heartbeat_url == "http://x"
    assert cfg.startup_notice is False and cfg.vid == 0x1234 and cfg.insecure_tls is True


def test_config_defaults_match_production(monkeypatch):
    monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    cfg = Config.from_env()
    assert cfg.delivery_alert_after == 900 and cfg.storage_alert_pct == 80 and cfg.heartbeat_url is None
    assert cfg.send_interval == 1 and cfg.startup_notice is True
