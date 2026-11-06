import logging
import pytest

from smsgw import atguard
from smsgw.atguard import ForbiddenCommand
from smsgw.chat import ChatClient, ChatError
from smsgw.main import Config, Gateway, NotReady, parse_cmgl
from smsgw.pdu import decode
from smsgw.store import Store
from smsgw.usbat import AtChannel
from tests.fakes import FakeChat, FakeModem
from tests.pdu_encode import deliver


@pytest.fixture
def env(tmp_path):
    chat_srv = FakeChat()
    modem = FakeModem()
    cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False, alert_after=0.0)
    store = Store(cfg.db_path)
    chat = ChatClient(chat_srv.url)
    gw = Gateway(cfg, store, chat, lambda: modem)
    yield gw, modem, chat_srv, store
    chat_srv.close()


# ---- decoding -----------------------------------------------------------
def test_decode_gsm7_ucs2_alnum_and_concat():
    s = decode(deliver("+14155550123", "Code 123456 [ok] €"))
    assert s.sender == "+14155550123" and s.text == "Code 123456 [ok] €"
    s = decode(deliver("+8613800138000", "验证码 8888 😀", alphabet="ucs2"))
    assert s.text == "验证码 8888 😀"
    a = decode(deliver("+1555", "A" * 10, concat=(7, 2, 1)))
    assert a.concat == (7, 2, 1)
    g = decode(deliver("+1555", "héllo wörld", concat=(1, 2, 2)))
    assert g.text == "héllo wörld"


def test_decode_alphanumeric_sender():
    pdu = "07914487200030230400" + "0ED4F29CEE0E" + "00" + "00" + "62106130451000" + "05" + "C8329BFD06"
    # build real vector: alnum sender 'Hello' TON=0b101 (0xD0)
    from tests.pdu_encode import pack, gsm7_septets, scts
    snd = pack(gsm7_septets("Google")).hex().upper()
    pdu = ("0791448720003023" + "04" + "0C" + "D0" + snd + "00" + "00" + scts() + "02" + "4F4B")
    s = decode(pdu)
    assert s.sender == "Google"


# ---- allow-list -----------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "AT+CMGS=\"+1555\"", "AT+CMGD=1", "AT+CMGW", "AT+CIMI", "AT+CGSN", "AT+CSIM=1,\"00\"",
    "AT+QCFG=\"usbnet\",1", "ATD123;", "AT+CPIN=\"1234\"", "AT+CMGL=4\rAT+CMGD=1", "AT\x1a", "AT+CFUN=0",
])
def test_forbidden_commands(cmd):
    with pytest.raises(ForbiddenCommand):
        atguard.check(cmd)


def test_allowed_commands():
    for c in ["AT", "ATE0", "AT+CMGF=0", "AT+CPIN?", 'AT+CPMS="ME"', "AT+CMGL=4", "AT+CSQ"]:
        atguard.check(c)


def test_channel_refuses_before_writing():
    m = FakeModem()
    with pytest.raises(ForbiddenCommand):
        AtChannel(m).command("AT+CMGD=1,4")
    assert m.sent == []


# ---- end to end -----------------------------------------------------------
def test_sms_forwarded_once_with_full_body(env):
    gw, modem, chat, store = env
    modem.add("ME", deliver("+14155550123", "Your code is 482910. <b>x</b>"))
    assert gw.poll_once() == 1
    assert gw.flush_outbox() == 1
    assert len(chat.messages) == 1
    assert "482910" in chat.messages[0] and "+14155550123" in chat.messages[0]
    assert "<" not in chat.messages[0]
    # polling again (same message still in modem) must not re-notify
    assert gw.poll_once() == 0
    gw.flush_outbox()
    assert len(chat.messages) == 1


def test_same_text_new_message_is_not_deduped_wrongly(env):
    gw, modem, chat, _ = env
    modem.add("ME", deliver("+1555", "hi"))
    gw.poll_once()
    modem.add("SM", deliver("+1555", "hi").replace("62", "63", 1) if False else deliver("+1666", "hi"))
    gw.poll_once(); gw.flush_outbox()
    assert len(chat.messages) == 2


def test_long_sms_assembled_out_of_order(env):
    gw, modem, chat, _ = env
    modem.add("ME", deliver("+1555", "world!", concat=(9, 2, 2)))
    gw.poll_once(); gw.flush_outbox()
    assert chat.messages == []          # waiting for part 1
    modem.add("ME", deliver("+1555", "Hello ", concat=(9, 2, 1)))
    gw.poll_once(); gw.flush_outbox()
    assert len(chat.messages) == 1 and "Hello world!" in chat.messages[0]


def test_incomplete_long_sms_flushed_after_timeout(env):
    gw, modem, chat, store = env
    modem.add("ME", deliver("+1555", "part one", concat=(3, 2, 1)))
    gw.poll_once(now=1000)
    assert store.flush_incomplete(120, now=1050) == 0
    assert store.flush_incomplete(120, now=1200) == 1
    gw.flush_outbox(now=1200)
    assert "缺少" in chat.messages[0] and "part one" in chat.messages[0]


def test_retry_with_backoff_then_success(tmp_path):
    srv = FakeChat(fail=2)
    try:
        cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False)
        store = Store(cfg.db_path)
        modem = FakeModem(); modem.add("ME", deliver("+1555", "retry me"))
        gw = Gateway(cfg, store, ChatClient(srv.url), lambda: modem)
        gw.poll_once(now=0)
        assert gw.flush_outbox(now=0) == 0          # fail #1 -> next try +30s
        assert gw.flush_outbox(now=10) == 0          # not due yet (no request made)
        assert gw.flush_outbox(now=31) == 0          # fail #2 -> +60s
        assert gw.flush_outbox(now=60) == 0          # not due
        assert gw.flush_outbox(now=92) == 1
        assert len(srv.messages) == 1 and store.pending_count() == 0
    finally:
        srv.close()


def test_rejected_success_false_is_failure(tmp_path):
    srv = FakeChat(reject=True)
    try:
        with pytest.raises(ChatError):
            ChatClient(srv.url).post("x")
    finally:
        srv.close()


def test_sim_not_ready(env):
    gw, modem, *_ = env
    modem.sim = "SIM PIN"
    with pytest.raises(NotReady):
        gw.poll_once()


def test_never_sends_forbidden_commands_during_normal_run(env):
    gw, modem, *_ = env
    modem.add("ME", deliver("+1555", "x"))
    gw.poll_once()
    assert set(modem.sent) <= {"ATE0", "AT+CMGF=0", "AT+CPIN?", 'AT+CPMS="ME"', 'AT+CPMS="SM"', "AT+CPMS?", "AT+CMGL=4"}


def test_first_run_skip_backlog(tmp_path):
    srv = FakeChat()
    try:
        cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False, first_run_backlog="skip")
        store = Store(cfg.db_path)
        modem = FakeModem(); modem.add("ME", deliver("+1555", "old"))
        gw = Gateway(cfg, store, ChatClient(srv.url), lambda: modem)
        gw.poll_once(); gw.flush_outbox()
        assert srv.messages == []
        modem.add("ME", deliver("+1666", "fresh"))
        gw.poll_once(); gw.flush_outbox()
        assert len(srv.messages) == 1 and "fresh" in srv.messages[0]
    finally:
        srv.close()


def test_state_survives_restart(tmp_path):
    srv = FakeChat()
    try:
        cfg = Config(db_path=str(tmp_path / "s.db"), startup_notice=False)
        modem = FakeModem(); modem.add("ME", deliver("+1555", "once"))
        for _ in range(2):
            store = Store(cfg.db_path)
            gw = Gateway(cfg, store, ChatClient(srv.url), lambda: modem)
            gw.poll_once(); gw.flush_outbox(); store.close()
        assert len(srv.messages) == 1
    finally:
        srv.close()


def test_alert_on_problem_and_recovery(env):
    gw, modem, chat, _ = env
    modem.sim = "NOT READY"
    try:
        gw.poll_once()
    except NotReady as e:
        gw._note_problem(100.0, str(e))
    assert any("异常" in m for m in chat.messages)
    n = len(chat.messages)
    gw._note_problem(200.0, "again")      # no duplicate alert
    assert len(chat.messages) == n
    gw._note_ok(300.0)
    assert any("恢复" in m for m in chat.messages)


# ---- secrets never leak ---------------------------------------------------
def test_logs_and_errors_never_contain_token_or_body(env, caplog):
    gw, modem, chat, _ = env
    caplog.set_level(logging.DEBUG)
    modem.add("ME", deliver("+14155550123", "TOPSECRETBODY"))
    gw.poll_once(); gw.flush_outbox()
    text = caplog.text
    assert "SECRETTOKEN" not in text and "TOPSECRETBODY" not in text and "4155550123" not in text.replace("***0123", "")


def test_error_message_hides_token():
    c = ChatClient("http://127.0.0.1:1/webapi/entry.cgi?api=SYNO.Chat.External&method=incoming&version=2&token=SECRETTOKEN")
    with pytest.raises(ChatError) as e:
        c.post("x")
    assert "SECRETTOKEN" not in str(e.value)


def test_webhook_url_validation_and_insecure_tls_scope():
    with pytest.raises(ChatError):
        ChatClient("ftp://x/y")
    with pytest.raises(ChatError):
        ChatClient("https://example.com/hook")
    with pytest.raises(ChatError):
        ChatClient("https://8.8.8.8/webapi?api=SYNO.Chat.External&token=a", insecure_tls=True)
    ChatClient("https://127.0.0.1/webapi?api=SYNO.Chat.External&token=a", insecure_tls=True)


def test_parse_cmgl_ignores_garbage():
    assert parse_cmgl(["+CMGL: 0,1,,20", "0791ABCD", "+CMGL: 1,1,,5", "not hex!"]) == ["0791ABCD"]
