"""smsgw: receive-only SMS gateway for the Baiwang/DJI cellular dongle (2ca3:4006).

Reads SMS over the module's USB AT interface (libusb, userspace) and forwards
them to a Synology Chat incoming webhook. It never sends or modifies SMS and
never touches eSIM profiles. It deletes SMS only if DELETE_DELIVERED=1 is set,
and then only messages every channel has confirmed.
"""

__version__ = "0.2.2"
