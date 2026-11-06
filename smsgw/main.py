"""Main loop: poll the modem for SMS, store, and forward to Synology Chat."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__
from .chat import ChatClient, ChatError, format_message
from .errors import DeliveryError
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
    startup_notice_gap: float = 3600.0      # crash loops must not flood the phone
    delivery_alert_after: float = 900.0     # alert when an SMS has waited this long
    alert_repeat: float = 86400.0           # remind while a problem lasts
    send_interval: float = 1.0              # pause between sends on one channel (backlogs hit rate limits)
    storage_alert_pct: int = 80
    storage_check_interval: float = 600.0
    health_file: str = "/tmp/smsgw-health.json"
    heartbeat_url: Optional[str] = None     # dead-man's switch: pinged only while everything works
    heartbeat_interval: float = 300.0
    delete_delivered: bool = False          # free modem storage once every channel confirmed a message

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
            startup_notice_gap=float(e.get("STARTUP_NOTICE_GAP", "3600")),
            delivery_alert_after=float(e.get("DELIVERY_ALERT_AFTER", "900")),
            alert_repeat=float(e.get("ALERT_REPEAT", "86400")),
            send_interval=float(e.get("SEND_INTERVAL", "1")),
            storage_alert_pct=_env_int("STORAGE_ALERT_PCT", 80),
            storage_check_interval=float(e.get("STORAGE_CHECK_INTERVAL", "600")),
            health_file=e.get("HEALTH_FILE", cls.health_file),
            heartbeat_url=e.get("HEARTBEAT_URL") or None,
            heartbeat_interval=float(e.get("HEARTBEAT_INTERVAL", "300")),
            delete_delivered=e.get("DELETE_DELIVERED", "0") == "1",
        )


class NotReady(Exception):
    pass


def mask(number: str) -> str:
    return ("***" + number[-4:]) if len(number) > 4 else "***"


_CMGL_INDEX_RE = re.compile(r"^\+CMGL:\s*(\d+)\s*,")
UNDECODABLE_TEXT = "[无法解码的短信]"


def parse_cmgl_entries(lines: List[str]) -> List[Tuple[Optional[int], str]]:
    """Return (storage index, PDU hex) pairs from an AT+CMGL=4 response."""
    entries = []
    it = iter(lines)
    for line in it:
        if line.startswith("+CMGL:"):
            pdu = next(it, "")
            if pdu and all(c in "0123456789abcdefABCDEF" for c in pdu):
                m = _CMGL_INDEX_RE.match(line)
                entries.append((int(m.group(1)) if m else None, pdu))
    return entries


def parse_cmgl(lines: List[str]) -> List[str]:
    """Return the PDU hex strings from an AT+CMGL=4 response."""
    return [pdu for _index, pdu in parse_cmgl_entries(lines)]


# The storage name is required: without one (the reply to a set command) the numbers mean something else.
_CPMS_RE = re.compile(r'^\+CPMS:\s*"?([A-Za-z]\w*)"?\s*,\s*(\d+)\s*,\s*(\d+)')


def parse_cpms(lines: List[str]) -> Optional[Tuple[int, int]]:
    """Return (used, total) of the current read storage from an AT+CPMS? response."""
    for line in lines:
        m = _CPMS_RE.match(line)
        if m:
            return int(m.group(2)), int(m.group(3))
    return None


class Channel:
    """One delivery channel with its own failure state.

    When the channel itself is down it is probed with growing pauses, and every
    pending message goes out as soon as a probe succeeds.
    """

    RETRY_BASE = 30.0
    RETRY_MAX = 300.0

    def __init__(self, name: str, bit: int, send_message: Callable, send_notice: Callable[[str], None]):
        self.name = name
        self.bit = bit
        self.send_message = send_message
        self.send_notice = send_notice
        self.failures = 0
        self.retry_at = 0.0
        self.last_error = ""
        self.last_send = 0.0

    def available(self, now: float) -> bool:
        return now >= self.retry_at

    def succeeded(self) -> None:
        self.failures = 0
        self.retry_at = 0.0
        self.last_error = ""

    def failed(self, now: float, error: DeliveryError) -> None:
        self.last_error = str(error)
        if error.transient:
            self.failures += 1
            self.retry_at = now + min(self.RETRY_BASE * 2 ** min(self.failures - 1, 20), self.RETRY_MAX)


class Gateway:
    CHAT_BIT, PUSH_BIT = 1, 2

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
        self._modem_ok = False
        self._last_storage_check: Optional[float] = None
        self.storage_usage: Dict[str, Tuple[int, int]] = {}
        self._last_prune = 0.0
        self._last_heartbeat = 0.0
        self._channels = [Channel("Chat", self.CHAT_BIT, self._chat_message, self.chat.post)]
        if telegram:
            self._channels.append(Channel("Telegram", self.PUSH_BIT, self._push_message,
                                          lambda text: telegram.post(text, "短信网关")))

    # -- modem side -----------------------------------------------------
    def _open(self) -> AtChannel:
        if self._at is None:
            self._transport = self._factory()
            self._at = AtChannel(self._transport, allow_delete=self.cfg.delete_delivered)
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
        t = time.time() if now is None else now
        check_usage = (self._last_storage_check is None
                       or t - self._last_storage_check >= self.cfg.storage_check_interval)
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
            entries = parse_cmgl_entries(lines)
            for _index, pdu in entries:
                if self.store.seen(pdu, now=now):
                    continue
                sms = self._decode(pdu, storage)
                if sms.kind != "deliver":
                    self.store.add(pdu, sms, notify=False, now=now)
                    continue
                keep = sms.text == UNDECODABLE_TEXT and sms.timestamp == ""
                if self.store.add(pdu, sms, notify=notify, now=now, keep=keep):
                    new += 1
                    log.info("new SMS from %s (%d chars)", mask(sms.sender), len(sms.text))
            deleted = self._delete_delivered(at, storage, entries)
            if check_usage or deleted:
                self._read_usage(at, storage)
        if not read_any:
            raise AtError("no SMS storage could be read")
        if check_usage:
            self._last_storage_check = t
        if first_run:
            self.store.set_meta("initialized", "1")
        self.store.flush_incomplete(self.cfg.incomplete_timeout, now=now)
        return new

    @staticmethod
    def _decode(pdu: str, storage: str) -> Sms:
        """Decoding may never stop the gateway: the PDU stays in the modem and would be retried forever."""
        try:
            return decode(pdu)
        except Exception as e:  # noqa: BLE001
            level = logging.WARNING if isinstance(e, PduError) else logging.ERROR
            log.log(level, "undecodable PDU in %s (%d bytes, %s); recording placeholder",
                    storage, len(pdu) // 2, type(e).__name__)
            return Sms(sender="", timestamp="", text=UNDECODABLE_TEXT, concat=None)

    def _delete_delivered(self, at: AtChannel, storage: str, entries: List[Tuple[Optional[int], str]]) -> int:
        """With DELETE_DELIVERED=1, free the slots of messages every channel has confirmed."""
        if not self.cfg.delete_delivered:
            return 0
        deleted = 0
        for index, pdu in entries:
            if index is None or not self.store.can_delete(pdu):
                continue
            try:
                at.command(f"AT+CMGD={index}")
            except AtError as e:
                log.warning("could not delete a delivered SMS from %s: %s", storage, e)
                break  # the modem is unhappy: try again on the next poll
            deleted += 1
        if deleted:
            log.info("deleted %d delivered SMS from %s", deleted, storage)
        return deleted

    def _read_usage(self, at: AtChannel, storage: str) -> None:
        try:
            usage = parse_cpms(at.command("AT+CPMS?"))
        except AtError as e:
            log.debug("usage of %s unavailable: %s", storage, e)
            return
        if usage:
            self.storage_usage[storage] = usage
            log.info("storage %s: %d/%d used", storage, *usage)

    # -- chat side --------------------------------------------------------
    @property
    def _full_mask(self) -> int:
        return sum(ch.bit for ch in self._channels)

    def _chat_message(self, m) -> None:
        self.chat.post(format_message(m.sender, m.ts, m.text, m.incomplete))

    def _push_message(self, m) -> None:
        self.telegram.post(*self._push_text(m))

    def _pace(self, ch: Channel) -> None:
        wait = ch.last_send + self.cfg.send_interval - time.monotonic()
        if ch.last_send and wait > 0:
            self._sleep(wait)
        ch.last_send = time.monotonic()

    def flush_outbox(self, now: Optional[float] = None) -> int:
        t = time.time() if now is None else now
        full = self._full_mask
        self.store.finalize(full, now=t)
        refused = set()  # a channel that refused a message gets no more this round (it may be rate limiting)
        sent = 0
        for m in self.store.due(now=t, channels=full):
            done = m.done
            for ch in self._channels:
                if self._stop or done & ch.bit or ch.bit in refused or not ch.available(t):
                    continue
                self._pace(ch)
                try:
                    ch.send_message(m)
                except DeliveryError as e:
                    ch.failed(t, e)
                    log.warning("%s delivery failed: %s", ch.name.lower(), e)
                    if not e.transient:
                        refused.add(ch.bit)
                        self.store.mark_failed(m.id, now=t)
                else:
                    ch.succeeded()
                    done |= ch.bit
                    self.store.mark_channel(m.id, ch.bit, full, now=t)
            if done & full == full:
                sent += 1
        return sent

    @staticmethod
    def _push_text(m) -> tuple:
        body = f"{m.ts.replace('T', ' ')}\n{m.text}" if m.ts else m.text
        if m.incomplete:
            body = "⚠️ 长短信缺少部分分段\n" + body
        return body, f"\U0001F4E9 {m.sender or '(unknown sender)'}"

    def _notice(self, text: str, now: Optional[float] = None) -> int:
        """Send a status message to every reachable channel; returns how many took it."""
        t = time.time() if now is None else now
        delivered = 0
        for ch in self._channels:
            if not ch.available(t):
                continue
            try:
                ch.send_notice(text)
            except DeliveryError as e:
                ch.failed(t, e)
                log.warning("%s notice not delivered: %s", ch.name.lower(), e)
            else:
                ch.succeeded()
                delivered += 1
        return delivered

    # -- supervision ------------------------------------------------------
    def _note_ok(self, now: float) -> None:
        if self._alerted:
            self._notice("✅ 短信网关已恢复正常", now=now)
        self._problem_since = None
        self._alerted = False
        self._backoff = 5.0

    def _note_problem(self, now: float, why: str) -> None:
        if self._problem_since is None:
            self._problem_since = now
        if not self._alerted and now - self._problem_since >= self.cfg.alert_after:
            minutes = int(self.cfg.alert_after // 60)
            self._alerted = self._notice(f"⚠️ 短信网关异常已持续 {minutes} 分钟以上:{why}", now=now) > 0

    def _alert_condition(self, key: str, active: bool, now: float, alert_text: Callable[[], str],
                         clear_text: str) -> None:
        """Alert once per ``alert_repeat`` while active, once when cleared. Survives restarts.

        ``<key>_seen`` records a problem even if no channel could carry the alert, so that
        after a total outage the user still learns why a burst of old messages arrived.
        """
        last = self.store.get_meta(key)
        seen = self.store.get_meta(f"{key}_seen")
        if active:
            if seen is None:
                self.store.set_meta(f"{key}_seen", str(now))
            if (last is None or now - float(last) >= self.cfg.alert_repeat) and self._notice(alert_text(), now=now):
                self.store.set_meta(key, str(now))
        elif (last is not None or seen is not None) and self._notice(clear_text, now=now):
            self.store.del_meta(key)
            self.store.del_meta(f"{key}_seen")

    def _delivery_overdue(self, now: float) -> bool:
        oldest = self.store.oldest_pending()
        return oldest is not None and now - oldest >= self.cfg.delivery_alert_after

    def _supervise_delivery(self, now: float) -> None:
        def text() -> str:
            minutes = int(self.cfg.delivery_alert_after // 60)
            states = ";".join(f"{ch.name}:{ch.last_error or '正常'}" for ch in self._channels)
            return f"⚠️ 短信转发异常:{self.store.pending_count()} 条短信超过 {minutes} 分钟未送达({states})"

        self._alert_condition("delivery_alert_at", self._delivery_overdue(now), now, text, "✅ 积压的短信已送达")

    def _supervise_storage(self, now: float) -> None:
        full = {s: u for s, u in self.storage_usage.items()
                if u[1] > 0 and u[0] * 100 >= u[1] * self.cfg.storage_alert_pct}

        def text() -> str:
            states = ",".join(f"{s} 已用 {u[0]}/{u[1]}" for s, u in full.items())
            return f"⚠️ 短信存储快满了({states})。存满后可能收不到新短信,请清理 SIM 卡上的旧短信。"

        self._alert_condition("storage_alert_at", bool(full), now, text, "✅ 短信存储空间已恢复")

    def _heartbeat(self, now: float) -> None:
        if (not self.cfg.heartbeat_url or not self._modem_ok or self._delivery_overdue(now)
                or now - self._last_heartbeat < self.cfg.heartbeat_interval):
            return
        self._last_heartbeat = now
        try:
            with urllib.request.urlopen(self.cfg.heartbeat_url, timeout=10) as resp:
                resp.read(64)
        except Exception as e:  # noqa: BLE001 - the URL is a secret: log only the type
            log.warning("heartbeat failed: %s", type(e).__name__)

    def _write_health(self, now: float) -> None:
        status = {
            "ts": now,
            "modem_ok": self._modem_ok,
            "problem_since": self._problem_since,
            "pending": self.store.pending_count(),
            "overdue": self._delivery_overdue(now),
            "alert_after": self.cfg.alert_after,
            "storage": {s: list(u) for s, u in self.storage_usage.items()},
        }
        tmp = f"{self.cfg.health_file}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.replace(tmp, self.cfg.health_file)

    def _unexpected(self, where: str, e: Exception) -> None:
        """Log where it happened without the exception text, which could quote an SMS."""
        frame = traceback.extract_tb(e.__traceback__)[-1] if e.__traceback__ else None
        place = f"{os.path.basename(frame.filename)}:{frame.lineno}" if frame else "?"
        log.error("unexpected %s during %s (%s)", type(e).__name__, where, place)
        try:
            self.store.rollback()
        except Exception:  # noqa: BLE001
            pass

    def _modem_step(self, t: float, now: Optional[float]) -> float:
        """One poll of the modem; returns how long to wait before the next one."""
        try:
            self.poll_once(now=now)
        except NotReady as e:
            self._modem_ok = False
            self._note_problem(t, str(e))
            log.warning("%s", e)
            return self.cfg.poll_interval
        except (UsbError, AtError, OSError) as e:
            self._close()
            self._modem_ok = False
            self._note_problem(t, type(e).__name__)
            log.warning("modem error: %s (retry in %.0fs)", e, self._backoff)
        except Exception as e:  # noqa: BLE001 - e.g. the disk is full: keep trying, keep delivering
            self._close()
            self._modem_ok = False
            self._unexpected("poll", e)
            self._note_problem(t, type(e).__name__)
        else:
            self._modem_ok = True
            self._note_ok(t)
            return self.cfg.poll_interval
        delay = self._backoff
        self._backoff = min(self._backoff * 2, 60.0)
        return delay

    def _prune(self, t: float) -> None:
        if t - self._last_prune > 3600:
            self.store.prune(self.cfg.retention_days, now=t)
            self._last_prune = t

    def tick(self, now: Optional[float] = None) -> float:
        """One full cycle. Each step is isolated: a failure in one must not stop the others,
        in particular delivering what is already stored while the modem is down."""
        t = time.time() if now is None else now
        delay = self._modem_step(t, now)
        steps = (("delivery", lambda: self.flush_outbox(now=now)),
                 ("delivery check", lambda: self._supervise_delivery(t)),
                 ("storage check", lambda: self._supervise_storage(t)),
                 ("prune", lambda: self._prune(t)),
                 ("heartbeat", lambda: self._heartbeat(t)),
                 ("health", lambda: self._write_health(t)))
        for name, step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001
                self._unexpected(name, e)
        return delay

    def _startup(self) -> None:
        self.store.reset_retries()  # whatever made messages back off may be gone by now
        if not self.cfg.startup_notice:
            return
        t = time.time()
        last = self.store.get_meta("startup_notice_at")
        if last is None or t - float(last) >= self.cfg.startup_notice_gap:
            if self._notice(f"\U0001F7E2 短信网关已启动 (v{__version__}),只收不发", now=t):
                self.store.set_meta("startup_notice_at", str(t))

    def run(self) -> None:
        try:
            self._startup()
        except Exception as e:  # noqa: BLE001
            self._unexpected("startup", e)
        while not self._stop:
            self._sleep(self.tick())
        self._close()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop and time.monotonic() < end:
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def stop(self, *_a) -> None:
        self._stop = True


def check_health(cfg: Config, now: Optional[float] = None) -> Tuple[int, str]:
    """Used by the Docker healthcheck. Returns (exit code, description)."""
    now = time.time() if now is None else now
    try:
        with open(cfg.health_file, "r", encoding="utf-8") as f:
            s = json.load(f)
        age = now - s["ts"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return 1, f"no health data ({type(e).__name__})"
    if age > max(180.0, cfg.poll_interval * 6):
        return 1, f"main loop stalled for {int(age)}s"
    if s.get("overdue"):
        return 1, f"{s.get('pending')} SMS waiting for delivery"
    since = s.get("problem_since")
    if since is not None and now - since >= s.get("alert_after", cfg.alert_after):
        return 1, "modem unavailable"
    return 0, f"ok ({s.get('pending')} pending)"


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
    if "--health" in argv:
        code, description = check_health(cfg)
        print(description)
        return code
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
    if cfg.delete_delivered:
        log.warning("DELETE_DELIVERED is on: SMS confirmed by every channel are deleted from the modem")
    gw.run()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
