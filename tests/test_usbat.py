"""Unit tests for the AT channel and the USB transport, with pyusb replaced by fakes."""
import pytest
import usb.core

from smsgw.main import parse_cmgl, parse_cpms
from smsgw.usbat import AtChannel, AtError, UsbError, UsbTransport


class ScriptedTransport:
    """Returns the scripted chunks in order, then times out."""

    def __init__(self, chunks=(), pending=()):
        self.chunks = list(chunks)
        self.pending = list(pending)     # readable before the command is written
        self.written = []

    def write(self, data):
        self.written.append(data)
        self.pending += self.chunks
        self.chunks = []

    def read(self, timeout_ms):
        return self.pending.pop(0) if self.pending else b""

    def close(self):
        pass


def test_response_split_across_reads_is_reassembled():
    t = ScriptedTransport([b"+CPIN", b": RE", b"ADY\r", b"\n\r\nO", b"K\r\n"])
    assert AtChannel(t).command("AT+CPIN?") == ["+CPIN: READY"]


def test_echo_and_blank_lines_are_ignored():
    t = ScriptedTransport([b"AT+CSQ\r\n\r\n+CSQ: 20,99\r\n\r\nOK\r\n"])
    assert AtChannel(t).command("AT+CSQ") == ["+CSQ: 20,99"]


def test_unsolicited_lines_before_a_command_are_kept_apart():
    t = ScriptedTransport([b"OK\r\n"], pending=[b"+CMTI: \"ME\",3\r\n"])
    ch = AtChannel(t)
    assert ch.command("AT") == []
    assert ch.urc == ['+CMTI: "ME",3']


def test_unsolicited_line_buffer_is_bounded():
    t = ScriptedTransport([b"OK\r\n"], pending=[b"".join(b"+URC: %d\r\n" % i for i in range(200))])
    ch = AtChannel(t)
    ch.command("AT")
    assert len(ch.urc) == 50


@pytest.mark.parametrize("line", [b"ERROR", b"+CME ERROR: 10", b"+CMS ERROR: 500", b"NO CARRIER"])
def test_error_finals_raise(line):
    with pytest.raises(AtError):
        AtChannel(ScriptedTransport([line + b"\r\n"])).command("AT")


def test_silent_modem_times_out():
    with pytest.raises(AtError, match="timeout"):
        AtChannel(ScriptedTransport()).command("AT", timeout=0.01)


def test_binary_noise_does_not_crash():
    t = ScriptedTransport([b"\xff\xfe garbage\r\nOK\r\n"])
    assert len(AtChannel(t).command("AT")) == 1


@pytest.mark.parametrize("lines,expected", [
    (['+CPMS: "ME",3,180,"ME",3,180,"ME",3,180'], (3, 180)),
    (['+CPMS: "SM", 12, 50, "SM", 12, 50'], (12, 50)),
    (["+CPMS: ME,1,2"], (1, 2)),
    ([], None), (["nonsense"], None), (["+CPMS: 3,180,3,180"], None),
])
def test_parse_cpms(lines, expected):
    assert parse_cpms(lines) == expected


def test_parse_cmgl_handles_truncated_response():
    assert parse_cmgl(["+CMGL: 0,1,,20"]) == []
    assert parse_cmgl([]) == []


# ---- UsbTransport ---------------------------------------------------------
class Ep:
    def __init__(self, addr, attrs=0x02):
        self.bEndpointAddress = addr
        self.bmAttributes = attrs


class Intf(list):
    def __init__(self, num, eps):
        super().__init__(eps)
        self.bInterfaceNumber = num
        self.bAlternateSetting = 0


class Dev:
    def __init__(self, interfaces, kernel_driver=False, cfg_error=None):
        self.interfaces = interfaces
        self.kernel_driver = kernel_driver
        self.cfg_error = cfg_error
        self.read_result = b"\x41"
        self.write_error = None

    def get_active_configuration(self):
        if self.cfg_error:
            raise self.cfg_error
        return self.interfaces

    def is_kernel_driver_active(self, iface):
        return self.kernel_driver

    def read(self, addr, size, timeout):
        if isinstance(self.read_result, Exception):
            raise self.read_result
        return self.read_result

    def write(self, addr, data, timeout):
        if self.write_error:
            raise self.write_error


GOOD = [Intf(2, [Ep(0x84), Ep(0x03)])]


@pytest.fixture
def patched(monkeypatch):
    calls = {"claimed": [], "released": []}
    monkeypatch.setattr(usb.util, "claim_interface", lambda d, i: calls["claimed"].append(i))
    monkeypatch.setattr(usb.util, "release_interface", lambda d, i: calls["released"].append(i))
    monkeypatch.setattr(usb.util, "dispose_resources", lambda d: None)

    def use(dev):
        monkeypatch.setattr(usb.core, "find", lambda **_k: dev)
        return calls

    return use


def test_missing_device_is_usb_error(patched):
    patched(None)
    with pytest.raises(UsbError, match="not found"):
        UsbTransport(0x2CA3, 0x4006, 2)


def test_missing_libusb_backend_is_usb_error_not_a_crash(monkeypatch):
    def no_backend(**_k):
        raise usb.core.NoBackendError("No backend available")

    monkeypatch.setattr(usb.core, "find", no_backend)
    with pytest.raises(UsbError):
        UsbTransport(0x2CA3, 0x4006, 2)


def test_usb_failure_while_opening_is_usb_error(patched):
    patched(Dev(GOOD, cfg_error=usb.core.USBError("gone")))
    with pytest.raises(UsbError):
        UsbTransport(0x2CA3, 0x4006, 2)


def test_missing_interface_or_endpoints_are_rejected(patched):
    patched(Dev([Intf(0, [])]))
    with pytest.raises(UsbError, match="interface 2 not present"):
        UsbTransport(0x2CA3, 0x4006, 2)
    patched(Dev([Intf(2, [Ep(0x84)])]))
    with pytest.raises(UsbError, match="bulk"):
        UsbTransport(0x2CA3, 0x4006, 2)


def test_refuses_to_fight_a_kernel_driver(patched):
    calls = patched(Dev(GOOD, kernel_driver=True))
    with pytest.raises(UsbError, match="kernel driver"):
        UsbTransport(0x2CA3, 0x4006, 2)
    assert calls["claimed"] == []


def test_read_write_close(patched):
    dev = Dev(GOOD)
    calls = patched(dev)
    t = UsbTransport(0x2CA3, 0x4006, 2)
    assert calls["claimed"] == [2]
    assert t.read(10) == b"\x41"
    t.write(b"AT\r")
    dev.read_result = usb.core.USBTimeoutError("timeout")
    assert t.read(10) == b""
    dev.read_result = usb.core.USBError("unplugged")
    with pytest.raises(UsbError):
        t.read(10)
    dev.write_error = usb.core.USBError("unplugged")
    with pytest.raises(UsbError):
        t.write(b"AT\r")
    t.close()
    t.close()
    assert calls["released"] == [2]
