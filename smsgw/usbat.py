"""USB AT channel.

``Transport`` is a tiny interface (write/read) so the AT logic can be tested
without hardware. ``UsbTransport`` implements it with pyusb/libusb by claiming
one vendor-specific interface of the dongle directly in userspace.
"""

from __future__ import annotations

import logging
import time
from typing import List, Protocol

from . import atguard

log = logging.getLogger("smsgw.usb")


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, timeout_ms: int) -> bytes:
        """Return available bytes, or b'' on timeout."""

    def close(self) -> None: ...


class UsbError(Exception):
    pass


class UsbTransport:
    def __init__(self, vid: int, pid: int, iface: int):
        import usb.core
        import usb.util

        self._usb_core = usb.core
        self._usb_util = usb.util
        try:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev is None:
                raise UsbError(f"USB device {vid:04x}:{pid:04x} not found")
            cfg = dev.get_active_configuration()
        except (ValueError, usb.core.USBError) as e:  # NoBackendError is a ValueError
            raise UsbError(f"cannot open USB device {vid:04x}:{pid:04x}: {type(e).__name__}") from e
        self._dev = dev
        intf = None
        for candidate in cfg:
            if candidate.bInterfaceNumber == iface and candidate.bAlternateSetting == 0:
                intf = candidate
                break
        if intf is None:
            raise UsbError(f"interface {iface} not present on {vid:04x}:{pid:04x}")
        ep_in = ep_out = None
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                ep_in = ep_in or ep
            else:
                ep_out = ep_out or ep
        if ep_in is None or ep_out is None:
            raise UsbError(f"interface {iface} has no bulk IN/OUT endpoint pair")
        self._iface = iface
        self._ep_in, self._ep_out = ep_in, ep_out
        # Never detach a kernel driver: if something else owns the interface we
        # refuse instead of fighting over it.
        try:
            if dev.is_kernel_driver_active(iface):
                raise UsbError(f"interface {iface} is owned by a kernel driver; refusing to detach it")
        except NotImplementedError:
            pass
        try:
            usb.util.claim_interface(dev, iface)
        except usb.core.USBError as e:
            raise UsbError(f"cannot claim interface {iface}: {e}") from e
        self._claimed = True
        log.info(
            "claimed interface %d (IN 0x%02x, OUT 0x%02x)",
            iface, ep_in.bEndpointAddress, ep_out.bEndpointAddress,
        )

    def write(self, data: bytes) -> None:
        try:
            self._dev.write(self._ep_out.bEndpointAddress, data, timeout=3000)
        except self._usb_core.USBError as e:
            raise UsbError(f"USB write failed: {e}") from e

    def read(self, timeout_ms: int) -> bytes:
        try:
            return bytes(self._dev.read(self._ep_in.bEndpointAddress, 4096, timeout=timeout_ms))
        except self._usb_core.USBTimeoutError:
            return b""
        except self._usb_core.USBError as e:
            raise UsbError(f"USB read failed: {e}") from e

    def close(self) -> None:
        if getattr(self, "_claimed", False):
            try:
                self._usb_util.release_interface(self._dev, self._iface)
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass
            self._claimed = False
        try:
            self._usb_util.dispose_resources(self._dev)
        except Exception:  # noqa: BLE001
            pass


class AtError(Exception):
    pass


class AtChannel:
    """Line-oriented AT command runner on top of a Transport."""

    FINAL_OK = ("OK",)
    FINAL_ERR_PREFIX = ("ERROR", "+CME ERROR", "+CMS ERROR", "NO CARRIER")

    def __init__(self, transport: Transport, allow_delete: bool = False):
        self._t = transport
        self._allow_delete = allow_delete
        self._buf = b""
        self.urc: List[str] = []

    def _drain(self) -> None:
        """Discard anything already pending (URCs between commands)."""
        while True:
            chunk = self._t.read(50)
            if not chunk:
                break
            self._buf += chunk
        if self._buf:
            for line in self._buf.decode("ascii", "replace").splitlines():
                line = line.strip()
                if line:
                    self.urc.append(line)
            self._buf = b""
        del self.urc[:-50]

    def command(self, cmd: str, timeout: float = 8.0) -> List[str]:
        """Send an allow-listed command; return response lines (without echo/OK)."""
        norm = atguard.check(cmd, allow_delete=self._allow_delete)
        self._drain()
        self._t.write(norm.encode("ascii") + b"\r")
        deadline = time.monotonic() + timeout
        lines: List[str] = []
        buf = b""
        while time.monotonic() < deadline:
            chunk = self._t.read(300)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip()
                if not line or line == norm:
                    continue
                if line in self.FINAL_OK:
                    return lines
                if line.startswith(self.FINAL_ERR_PREFIX):
                    raise AtError(f"{norm.split('=')[0]} -> {line}")
                lines.append(line)
        raise AtError(f"timeout waiting for response to {norm.split('=')[0]}")
