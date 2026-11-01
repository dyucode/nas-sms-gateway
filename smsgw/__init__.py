"""smsgw: receive-only SMS gateway for the DJI first-generation 4G cellular module.

Reads SMS over the module's USB AT interface (libusb, userspace) and forwards
them to a Synology Chat incoming webhook. It never sends, deletes or modifies
SMS and never touches eSIM profiles.
"""

__version__ = "0.2.0"
