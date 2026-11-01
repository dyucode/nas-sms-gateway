"""Main loop: poll the modem for SMS, store, and forward to Synology Chat."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import __version__
from .chat import ChatClient, ChatError, format_message
from .pdu import PduError, Sms, decode
from .telegram import TelegramClient, TelegramError, find_chat_ids
from .store import Store
from .usbat import AtChannel, AtError, Transport, UsbError, UsbTransport

log = logging.getLogger("smsgw")


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass
class Config:
    webhook_file: str = "/run/secrets/chat_webhook_url"
    ca_file: Optional[str] = None
    telegram_file: str = "/run/secrets/telegram"
    insecure_tls: bool = False
    vid: int = 0x2CA3
    pid: int = 0x4006
    iface: int = 2
    poll_interval: float = 10.0
    storages: tuple = ("ME", "SM")
    db_path: str = "/data/sms.db"
    retention_days: int = 14
    incomplete_timeout: float = 120.0
    first_run_backlog: str = "notify"  # or "skip"
    alert_after: float = 300.0
    startup_notice: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        return cls(
            webhook_file=e.get("CHAT_WEBHOOK_URL_FILE", cls.webhook_file),
            ca_file=e.get("CHAT_CA_FILE") or None,
            telegram_file=e.get("TELEGRAM_FILE", cls.telegram_file),
            insecure_tls=e.get("CHAT_INSECURE_TLS", "0") == "1",
            vid=int(e.get("USB_VID", "2ca3"), 16),
            pid=int(e.get("USB_PID", "4006"), 16),
            iface=_env_int("USB_IFACE", 2),
            poll_interval=float(e.get("POLL_INTERVAL", "10")),
            storages=tuple(s.strip().upper() for s in e.get("STORAGES", "ME,SM").split(",") if s.strip()),
            db_path=e.get("DB_PATH", cls.db_path),
            retention_days=_env_int("RETENTION_DAYS", 14),
            incomplete_timeout=float(e.get("INCOMPLETE_TIMEOUT", "120")),
            first_run_backlog=e.get("FIRST_RUN_BACKLOG", "notify").lower(),
            alert_after=float(e.get("ALERT_AFTER", "300")),
            startup_notice=e.get("STARTUP_NOTICE", "1") == "1",
        )


class NotReady(Exception):
    pass


def mask(number: str) -> str:
    return ("***" + number[-4:]) if len(number) > 4 else "***"


def parse_cmgl(lines: List[str]) -> List[str]:
    """Return the PDU hex strings from an AT+CMGL=4 response."""
    pdus = []
    it = iter(lines)
    for line in it:
        if line.startswith("+CMGL:"):
            pdu = next(it, "")
            if pdu and all(c in "0123456789abcdefABCDEF" for c in pdu):
                pdus.append(pdu)
    return pdus


class Gateway:
    def __init__(self, cfg: Config, store: Store, chat: ChatClient, transport_factory: Callable[[], Transport],
                 telegram: Optional[TelegramClient] = None):
        self.cfg = cfg
        self.store = store
        self.chat = chat
        self.telegram = telegram
        self._factory = transport_factory
        self._transport: Optional[Transport] = None
        self._at: Optional[AtChannel] = None
        self._problem_since: Optional[float] = None
        self._alerted = False
        self._backoff = 5.0
        self._stop = False

    # -- modem side -----------------------------------------------------
    def _open(self) -> AtChannel:
        if self._at is None:
            self._transport = self._factory()
            self._at = AtChannel(self._transport)
            self._at.command("ATE0")
            self._at.command("AT+CMGF=0")
            log.info("modem channel ready")
        return self._at

    def _close(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
        self._transport = None
        self._at = None

    def poll_once(self, now: Optional[float] = None) -> int:
        """Read all messages once. Returns number of new messages recorded."""
        at = self._open()
        pin = at.command("AT+CPIN?")
        if not any(l.replace(" ", "").upper() == "+CPIN:READY" for l in pin):
            raise NotReady("SIM not ready")
        first_run = self.store.get_meta("initialized") is None
        notify = not (first_run and self.cfg.first_run_backlog == "skip")
        new = 0
        read_any = False
        for storage in self.cfg.storages:
            try:
                at.command(f'AT+CPMS="{storage}"')
                lines = at.command("AT+CMGL=4", timeout=20)
            except AtError as e:
                log.debug("storage %s skipped: %s", storage, e)
                continue
            read_any = True
            for pdu in parse_cmgl(lines):
                if self.store.seen(pdu):
                    continue
                try:
                    sms = decode(pdu)
                except PduError:
                    log.warning("undecodable PDU in %s (%d bytes); recording placeholder", storage, len(pdu) // 2)
                    sms = Sms(sender="", timestamp="", text="[无法解码的短信]", concat=None)
                if sms.kind != "deliver":
                    self.store.add(pdu, sms, notify=False, now=now)
                    continue
                if self.store.add(pdu, sms, notify=notify, now=now):
                    new += 1
                    log.info("new SMS from %s (%d chars)", mask(sms.sender), len(sms.text))
        if not read_any:
            raise AtError("no SMS storage could be read")
        if first_run:
            self.store.set_meta("initialized", "1")
        self.store.flush_incomplete(self.cfg.incomplete_timeout, now=now)
        return new

    # -- chat side --------------------------------------------------------
    CHAT_BIT, PUSH_BIT = 1, 2

    @property
    def _full_mask(self) -> int:
        return self.CHAT_BIT | (self.PUSH_BIT if self.telegram else 0)

    def flush_outbox(self, now: Optional[float] = None) -> int:
        sent = 0
        for m in self.store.due(now=now):
            failed = False
            if not m.done & self.CHAT_BIT:
                try:
                    self.chat.post(format_message(m.sender, m.ts, m.text, m.incomplete))
                    self.store.mark_channel(m.id, self.CHAT_BIT, self._full_mask, now=now)
                except ChatError as e:
                    failed = True
                    log.warning("chat delivery failed (attempt %d): %s", m.attempts + 1, e)
            if self.telegram and not m.done & self.PUSH_BIT:
                try:
                    self.telegram.post(*self._push_text(m))
                    self.store.mark_channel(m.id, self.PUSH_BIT, self._full_mask, now=now)
                except TelegramError as e:
                    failed = True
                    log.warning("telegram delivery failed (attempt %d): %s", m.attempts + 1, e)
            if failed:
                self.store.mark_failed(m.id, now=now)
                break
            self.store.mark_channel(m.id, 0, self._full_mask, now=now)
            sent += 1
        return sent

    @staticmethod
    def _push_text(m) -> tuple:
        body = f"{m.ts.replace('T', ' ')}\n{m.text}" if m.ts else m.text
        if m.incomplete:
            body = "⚠️ 长短信缺少部分分段\n" + body
        return body, f"\U0001F4E9 {m.sender or '(unknown sender)'}"

    def _notice(self, text: str) -> None:
        try:
            self.chat.post(text)
        except ChatError as e:
            log.warning("notice not delivered: %s", e)
        if self.telegram:
            try:
                self.telegram.post(text, "短信网关")
            except TelegramError as e:
                log.warning("telegram notice not delivered: %s", e)

    # -- supervision ------------------------------------------------------
    def _note_ok(self, now: float) -> None:
        if self._alerted:
            self._notice("✅ 短信网关已恢复正常")
        self._problem_since = None
        self._alerted = False
        self._backoff = 5.0

    def _note_problem(self, now: float, why: str) -> None:
        if self._problem_since is None:
            self._problem_since = now
        if not self._alerted and now - self._problem_since >= self.cfg.alert_after:
            self._alerted = True
            self._notice(f"⚠️ 短信网关异常已持续 {int(self.cfg.alert_after // 60)} 分钟以上:{why}")

    def run(self) -> None:
        if self.cfg.startup_notice:
            self._notice(f"\U0001F7E2 短信网关已启动 (v{__version__}),只收不发")
        last_prune = 0.0
        while not self._stop:
            now = time.time()
            try:
                self.poll_once()
                self._note_ok(now)
            except NotReady as e:
                self._note_problem(now, str(e))
                log.warning("%s", e)
            except (UsbError, AtError, OSError) as e:
                self._close()
                self._note_problem(now, type(e).__name__)
                log.warning("modem error: %s (retry in %.0fs)", e, self._backoff)
                self._sleep(self._backoff)
                self._backoff = min(self._backoff * 2, 60.0)
                continue
            self.flush_outbox()
            if now - last_prune > 3600:
                self.store.prune(self.cfg.retention_days)
                last_prune = now
            self._sleep(self.cfg.poll_interval)
        self._close()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def stop(self, *_a) -> None:
        self._stop = True


def list_usb(cfg: Config) -> int:
    """Print interface/endpoint descriptors of the dongle (no AT traffic)."""
    import usb.core
    import usb.util

    dev = usb.core.find(idVendor=cfg.vid, idProduct=cfg.pid)
    if dev is None:
        print(f"{cfg.vid:04x}:{cfg.pid:04x} not found")
        return 1
    print(f"found {cfg.vid:04x}:{cfg.pid:04x}")
    for intf in dev.get_active_configuration():
        eps = ", ".join(
            f"0x{e.bEndpointAddress:02x}/{['ctrl','iso','bulk','int'][usb.util.endpoint_type(e.bmAttributes)]}" for e in intf
        )
        print(f"  iface {intf.bInterfaceNumber} class={intf.bInterfaceClass:02x} sub={intf.bInterfaceSubClass:02x} "
              f"proto={intf.bInterfaceProtocol:02x} endpoints: {eps}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    if "--list-usb" in argv:
        return list_usb(cfg)
    if "--telegram-chat-id" in argv:
        try:
            ids = find_chat_ids(cfg.telegram_file, ca_file=cfg.ca_file)
        except TelegramError as e:
            print(f"error: {e}")
            return 1
        print("\n".join(ids) if ids else "no messages found: send any message to your bot first, then retry")
        return 0 if ids else 1
    try:
        with open(cfg.webhook_file, "r", encoding="utf-8") as f:
            url = f.read()
        chat = ChatClient(url, ca_file=cfg.ca_file, insecure_tls=cfg.insecure_tls)
    except (OSError, ChatError) as e:
        log.error("cannot start: %s", e)
        return 2
    telegram = None
    if os.path.exists(cfg.telegram_file) and os.path.getsize(cfg.telegram_file) > 0:
        try:
            telegram = TelegramClient.from_file(cfg.telegram_file, ca_file=cfg.ca_file)
        except TelegramError as e:
            log.error("Telegram disabled, continuing with Chat only: %s", e)
        else:
            log.info("Telegram notifications enabled")
    os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
    store = Store(cfg.db_path)
    gw = Gateway(cfg, store, chat, lambda: UsbTransport(cfg.vid, cfg.pid, cfg.iface), telegram)
    signal.signal(signal.SIGTERM, gw.stop)
    signal.signal(signal.SIGINT, gw.stop)
    log.info("smsgw %s starting (receive-only); delivering to Chat at %s", __version__, chat.host)
    gw.run()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
