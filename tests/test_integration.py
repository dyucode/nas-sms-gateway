"""Integration tests: the real main loop, AT channel, SQLite file and HTTP clients together.

Only the USB dongle is simulated. Each scenario waits on observable results instead of sleeping.
"""
import os
import random
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from smsgw import __version__
from smsgw.chat import ChatClient
from smsgw.main import Channel, Config, Gateway
from smsgw.store import Store
from smsgw.telegram import TelegramClient
from smsgw.usbat import UsbError
from tests.fakes import FakeChat, FakeModem, FakeTelegram
from tests.pdu_encode import deliver, scts

pytestmark = pytest.mark.integration

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POISON_PDU = "00" + "44" + "04812143" + "00" + "08" + scts() + "03" + "020003"


def wait_for(condition, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class Running:
    """A gateway running its real loop in a thread, with its own SQLite connection."""

    def __init__(self, cfg, chat_url, tg_url, factory):
        self.ready = threading.Event()

        def runner():
            store = Store(cfg.db_path)
            tc = TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=tg_url) if tg_url else None
            self.gw = Gateway(cfg, store, ChatClient(chat_url), factory, tc)
            self.ready.set()
            self.gw.run()
            store.close()

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def stop(self):
        self.gw.stop()
        self.thread.join(20)
        assert not self.thread.is_alive(), "gateway did not stop"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(Channel, "RETRY_BASE", 0.05)
    monkeypatch.setattr(Channel, "RETRY_MAX", 0.2)
    chat, tg, modem = FakeChat(), FakeTelegram(), FakeModem()
    running = []

    class World:
        def __init__(self):
            self.chat, self.tg, self.modem = chat, tg, modem
            self.db_path = str(tmp_path / "data" / "sms.db")
            os.makedirs(os.path.dirname(self.db_path))

        def config(self, **kw):
            kw.setdefault("poll_interval", 0.02)
            kw.setdefault("send_interval", 0)
            kw.setdefault("startup_notice", False)
            kw.setdefault("health_file", str(tmp_path / "health.json"))
            return Config(db_path=self.db_path, **kw)

        def start(self, factory=None, telegram=True, **kw):
            r = Running(self.config(**kw), chat.url, tg.url if telegram else None, factory or (lambda: modem))
            running.append(r)
            return r

        def bodies(self, srv):
            return [m.split("\n")[-1] for m in srv.messages if "\U0001F4E9" in m or "\U0001F4E9" in m.split("\n")[0]]

    yield World()
    for r in running:
        if r.thread.is_alive():
            r.stop()
    chat.close()
    tg.close()


def test_sms_reaches_both_channels_through_the_real_loop(world):
    world.start()
    world.modem.add("ME", deliver("+14155550123", "Your code is 482910"))
    wait_for(lambda: world.chat.messages and world.tg.messages, what="delivery")
    time.sleep(0.2)                                  # further polls must not deliver it again
    assert len(world.chat.messages) == 1 and len(world.tg.messages) == 1
    assert "482910" in world.chat.messages[0] and "482910" in world.tg.messages[0]


def test_total_network_outage_and_recovery(world):
    """A firewall blocks the container: SMS arrive, both channels are down, then it is fixed."""
    world.chat.fail = world.tg.fail = 10 ** 9
    world.start(delivery_alert_after=0.1, alert_repeat=3600)
    for i in range(5):
        world.modem.add("ME", deliver("+1555", f"during outage {i}"))
    store = sqlite3.connect(world.db_path)
    count = lambda: store.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]
    wait_for(lambda: count() == 5, what="messages stored during the outage")
    time.sleep(0.3)
    assert world.chat.messages == [] and world.tg.messages == []

    # The firewall is fixed. Everything arrives, and the user is told.
    world.chat.fail = world.tg.fail = 0
    wait_for(lambda: count() == 0, what="backlog delivered after recovery")
    for srv in (world.chat, world.tg):
        delivered = [m for m in srv.messages if "during outage" in m]
        assert sorted(m.split("\n")[-1] for m in delivered) == [f"during outage {i}" for i in range(5)]
    wait_for(lambda: any("✅" in m for m in world.tg.messages), what="recovery notice")
    store.close()


def test_chat_down_user_is_warned_on_telegram(world):
    world.chat.fail = 10 ** 9
    world.start(delivery_alert_after=0.1, alert_repeat=3600)
    world.modem.add("ME", deliver("+1555", "hello"))
    wait_for(lambda: any("短信转发异常" in m for m in world.tg.messages), what="alert on Telegram")
    assert any("hello" in m for m in world.tg.messages)
    world.chat.fail = 0
    wait_for(lambda: any("hello" in m for m in world.chat.messages), what="Chat catching up")
    assert sum("hello" in m for m in world.tg.messages) == 1


def test_dongle_unplugged_and_replugged(world):
    state = {"plugged": True, "opens": 0}

    class Dongle:
        def write(self, data):
            if not state["plugged"]:
                raise UsbError("unplugged")
            world.modem.write(data)

        def read(self, timeout_ms):
            if not state["plugged"]:
                raise UsbError("unplugged")
            return world.modem.read(timeout_ms)

        def close(self):
            pass

    def factory():
        if not state["plugged"]:
            raise UsbError("not found")
        state["opens"] += 1
        return Dongle()

    r = world.start(factory=factory, alert_after=0.05)
    world.modem.add("ME", deliver("+1555", "before"))
    wait_for(lambda: any("before" in m for m in world.chat.messages), what="first message")
    state["plugged"] = False
    wait_for(lambda: any("异常" in m for m in world.tg.messages), what="modem alert")
    world.modem.add("ME", deliver("+1555", "after"))
    r.gw._backoff = 0.05                             # do not wait out the real backoff
    state["plugged"] = True
    wait_for(lambda: any("after" in m for m in world.chat.messages), what="message after replug", timeout=40)
    assert state["opens"] >= 2
    wait_for(lambda: any("恢复" in m for m in world.tg.messages), what="recovery notice")
    assert sum("before" in m for m in world.chat.messages) == 1


def test_poison_sms_does_not_stop_later_messages(world):
    world.start()
    world.modem.add("ME", POISON_PDU)
    world.modem.add("ME", deliver("+1555", "after the poison"))
    wait_for(lambda: any("after the poison" in m for m in world.chat.messages), what="later message")
    assert any("无法解码" in m for m in world.chat.messages)


def test_restart_during_backlog_delivers_exactly_once(world):
    world.chat.fail = world.tg.fail = 10 ** 9
    r = world.start()
    for i in range(10):
        world.modem.add("ME", deliver("+1555", f"sms {i}"))
    store = sqlite3.connect(world.db_path)
    wait_for(lambda: store.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 10, what="messages stored")
    store.close()
    r.stop()
    world.chat.fail = world.tg.fail = 0
    world.start()
    expected = [f"sms {i}" for i in range(10)]
    for srv in (world.chat, world.tg):
        wait_for(lambda s=srv: len(s.messages) >= 10, what="backlog after restart")
    time.sleep(0.2)
    assert sorted(m.split("\n")[-1] for m in world.chat.messages) == expected
    assert sorted(m.split("\n")[-1] for m in world.tg.messages) == expected


def test_upgrade_with_stuck_messages_from_0_2_0(world):
    """Messages stuck after a long outage: 131 attempts and a retry 30 minutes away."""
    db = sqlite3.connect(world.db_path)
    db.executescript("""
    CREATE TABLE raw_parts(hash TEXT PRIMARY KEY, sender TEXT, ts TEXT, text TEXT, ref INTEGER, total INTEGER,
      seq INTEGER, seen_at REAL NOT NULL, composed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, ts TEXT, text TEXT,
      incomplete INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, sent_at REAL,
      attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    future = time.time() + 1800
    db.execute("INSERT INTO meta VALUES('initialized','1')")
    db.execute("INSERT INTO outbox(sender,ts,text,created_at,attempts,next_try,done) VALUES('+1','','both missing',0,131,?,0)", (future,))
    db.execute("INSERT INTO outbox(sender,ts,text,created_at,attempts,next_try,done) VALUES('+2','','chat missing',0,105,?,2)", (future,))
    db.commit()
    db.close()
    world.start()
    wait_for(lambda: len(world.chat.messages) >= 2, what="stuck messages delivered right after the upgrade")
    assert [m.split("\n")[-1] for m in world.tg.messages if "missing" in m] == ["both missing"]


def test_soak_flaky_channels_every_message_exactly_once(world):
    """Hundreds of short and long SMS, out of order, in two storages, on unreliable channels."""
    rng = random.Random(7)
    world.modem.capacity = 1000
    world.chat.flaky_every = 7
    world.tg.flaky_every = 5
    world.start()
    expected = set()
    pdus = []
    for i in range(120):
        expected.add(f"short{i}")
        pdus.append(deliver("+1555", f"short{i}"))
    for i in range(40):
        ref = i + 1
        expected.add(f"long{i}-a|long{i}-b|long{i}-c")
        pdus += [deliver("+1666", f"long{i}-a|", concat=(ref, 3, 1)),
                 deliver("+1666", f"long{i}-b|", concat=(ref, 3, 2)),
                 deliver("+1666", f"long{i}-c", concat=(ref, 3, 3))]
    rng.shuffle(pdus)
    for pdu in pdus:
        world.modem.add(rng.choice(["ME", "SM"]), pdu)

    got = lambda srv: [m.split("\n")[-1] for m in srv.messages if "短信" not in m.split("\n")[0]]
    wait_for(lambda: len(got(world.chat)) >= len(expected), timeout=120, what="all messages on Chat")
    wait_for(lambda: len(got(world.tg)) >= len(expected), timeout=120, what="all messages on Telegram")
    time.sleep(0.3)
    assert sorted(got(world.chat)) == sorted(expected)
    assert sorted(got(world.tg)) == sorted(expected)


# ---- the real process -----------------------------------------------------
def process_env(tmp_path, webhook_url):
    webhook = tmp_path / "webhook.txt"
    webhook.write_text(webhook_url)
    env = dict(os.environ, PYTHONPATH=ROOT, CHAT_WEBHOOK_URL_FILE=str(webhook), TELEGRAM_FILE=str(tmp_path / "none"),
               DB_PATH=str(tmp_path / "data" / "sms.db"), HEALTH_FILE=str(tmp_path / "health.json"),
               POLL_INTERVAL="0.1", ALERT_AFTER="3600", USB_VID="ffff", USB_PID="ffff")
    return env


def run_cli(args, env):
    return subprocess.run([sys.executable, "-m", "smsgw", *args], env=env, capture_output=True, text=True, timeout=60)


def test_real_process_starts_without_a_dongle_and_reports_health(tmp_path):
    srv = FakeChat()
    env = process_env(tmp_path, srv.url)
    assert run_cli(["--health"], env).returncode == 1          # nothing has run yet
    proc = subprocess.Popen([sys.executable, "-m", "smsgw"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for(lambda: any("已启动" in m and __version__ in m for m in srv.messages), timeout=30, what="startup notice")
        wait_for(lambda: os.path.exists(env["HEALTH_FILE"]), timeout=30, what="health file")
        assert proc.poll() is None, "process died"
        result = run_cli(["--health"], env)
        assert result.returncode == 0 and "ok" in result.stdout
    finally:
        proc.terminate()
        output = proc.communicate(timeout=30)[0]
        srv.close()
    assert "SECRETTOKEN" not in output
    assert f"smsgw {__version__} starting" in output


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM is a hard kill on Windows")
def test_real_process_stops_cleanly_on_sigterm(tmp_path):
    srv = FakeChat()
    env = process_env(tmp_path, srv.url)
    proc = subprocess.Popen([sys.executable, "-m", "smsgw"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: srv.messages, timeout=30, what="startup notice")
        proc.terminate()
        assert proc.wait(timeout=15) == 0
    finally:
        proc.kill()
        srv.close()


def test_real_process_refuses_to_start_with_a_bad_webhook(tmp_path):
    env = process_env(tmp_path, "https://example.com/not-a-webhook?secret=1")
    result = run_cli([], env)
    assert result.returncode == 2
    assert "secret=1" not in result.stdout + result.stderr


def test_real_process_refuses_to_start_without_a_webhook_file(tmp_path):
    env = process_env(tmp_path, "x")
    env["CHAT_WEBHOOK_URL_FILE"] = str(tmp_path / "missing.txt")
    assert run_cli([], env).returncode == 2


def test_invalid_telegram_file_falls_back_to_chat_only(tmp_path):
    srv = FakeChat()
    env = process_env(tmp_path, srv.url)
    bad = tmp_path / "telegram.txt"
    bad.write_text("not a token\n")
    env["TELEGRAM_FILE"] = str(bad)
    proc = subprocess.Popen([sys.executable, "-m", "smsgw"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_for(lambda: srv.messages, timeout=30, what="startup notice on Chat")
    finally:
        proc.terminate()
        output = proc.communicate(timeout=30)[0]
        srv.close()
    assert "Telegram disabled" in output
