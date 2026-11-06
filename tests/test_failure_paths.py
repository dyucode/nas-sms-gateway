"""Error paths that only run when something is already wrong, so they must be tested on purpose."""
import pytest

from smsgw.chat import ChatClient, ChatError
from smsgw.main import Config, Gateway, list_usb, main
from smsgw.store import Store
from smsgw.telegram import TelegramClient, TelegramError, find_chat_ids, parse_token
from smsgw.usbat import AtError
from tests.fakes import FakeChat, FakeModem, FakeTelegram
from tests.pdu_encode import deliver


@pytest.fixture
def chat():
    srv = FakeChat()
    yield srv
    srv.close()


def gateway(tmp_path, srv, modem, **kw):
    cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False, send_interval=0,
                 health_file=str(tmp_path / "h.json"), **kw)
    return Gateway(cfg, Store(cfg.db_path), ChatClient(srv.url), lambda: modem)


def test_unreadable_storage_is_skipped_and_the_other_is_still_read(tmp_path, chat):
    modem = FakeModem()
    del modem.storages["SM"]                         # this modem answers +CMS ERROR for SM
    modem.add("ME", deliver("+1555", "from ME"))
    gw = gateway(tmp_path, chat, modem, storages=("SM", "ME"))
    assert gw.poll_once(now=0) == 1
    assert list(gw.storage_usage) == ["ME"]


def test_all_storages_unreadable_is_a_modem_error(tmp_path, chat):
    modem = FakeModem()
    modem.storages = {}
    gw = gateway(tmp_path, chat, modem)
    with pytest.raises(AtError, match="no SMS storage"):
        gw.poll_once(now=0)
    assert gw.tick(now=0) == 5.0                     # handled by the loop: retries with backoff
    assert gw._modem_ok is False


def test_transport_close_failure_is_ignored(tmp_path, chat):
    modem = FakeModem()

    def broken_close():
        raise OSError("already gone")

    modem.close = broken_close
    gw = gateway(tmp_path, chat, modem)
    gw.poll_once(now=0)
    gw._close()
    assert gw._at is None and gw._transport is None


def test_status_report_is_stored_but_never_forwarded(tmp_path, chat):
    modem = FakeModem()
    modem.add("ME", "00" + "02" + "00" * 20)
    gw = gateway(tmp_path, chat, modem)
    assert gw.poll_once(now=0) == 0
    assert gw.store.pending_count() == 0
    assert gw.poll_once(now=1) == 0


def test_startup_failure_does_not_prevent_running(tmp_path, chat, monkeypatch):
    gw = gateway(tmp_path, chat, FakeModem())

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(gw.store, "reset_retries", boom)
    monkeypatch.setattr(gw, "tick", lambda: gw.stop() or 0)
    gw.run()


def test_chat_non_json_and_non_object_replies_are_refusals():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    replies = [b"not json", b"[1,2]", b'{"success": false, "error": "text"}']

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(replies.pop(0))

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = ChatClient(f"http://127.0.0.1:{srv.server_port}/x?api=SYNO.Chat.External&token=T")
        for _ in range(3):
            with pytest.raises(ChatError) as e:
                c.post("x")
            assert e.value.transient is False and "code" not in str(e.value)
    finally:
        srv.shutdown()


def test_chat_413_is_about_the_message(chat, monkeypatch):
    import urllib.error
    import urllib.request

    def too_big(*_a, **_k):
        raise urllib.error.HTTPError("u", 413, "too big", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", too_big)
    with pytest.raises(ChatError) as e:
        ChatClient(chat.url).post("x")
    assert e.value.transient is False


def test_telegram_secret_file_errors(tmp_path):
    with pytest.raises(TelegramError, match="cannot read"):
        TelegramClient.from_file(str(tmp_path / "missing"))
    with pytest.raises(TelegramError, match="cannot read"):
        find_chat_ids(str(tmp_path / "missing"))
    with pytest.raises(TelegramError):
        parse_token("")
    with pytest.raises(TelegramError, match="invalid"):
        TelegramClient("bad", "1")


def test_telegram_non_json_reply_is_a_refusal(monkeypatch):
    srv = FakeTelegram()
    try:
        import smsgw.telegram as tg
        monkeypatch.setattr(tg.json, "loads", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad")))
        with pytest.raises(TelegramError) as e:
            TelegramClient(FakeTelegram.TOKEN, FakeTelegram.CHAT, api=srv.url).post("x")
        assert e.value.transient is False
    finally:
        srv.close()


def test_find_chat_ids_ignores_updates_without_a_chat(tmp_path):
    srv = FakeTelegram(updates=[{"update_id": 1}, {"channel_post": {"chat": {"id": -5, "type": "channel", "title": "T"}}}])
    try:
        f = tmp_path / "t.txt"
        f.write_text(FakeTelegram.TOKEN)
        assert find_chat_ids(str(f), api=srv.url) == ["-5 (channel, T)"]
    finally:
        srv.close()


# ---- command line ---------------------------------------------------------
def test_cli_health_and_chat_id_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HEALTH_FILE", str(tmp_path / "none.json"))
    monkeypatch.setenv("TELEGRAM_FILE", str(tmp_path / "none.txt"))
    assert main(["--health"]) == 1
    assert "no health data" in capsys.readouterr().out
    assert main(["--telegram-chat-id"]) == 1
    assert "error" in capsys.readouterr().out


def test_cli_list_usb_without_device(monkeypatch, capsys):
    import usb.core
    monkeypatch.setattr(usb.core, "find", lambda **_k: None)
    assert list_usb(Config()) == 1
    assert "not found" in capsys.readouterr().out
