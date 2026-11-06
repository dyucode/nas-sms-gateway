"""Errors shared by the delivery channels."""

from __future__ import annotations


class DeliveryError(Exception):
    """A notification could not be delivered.

    ``transient=True``  the channel itself is unavailable (network, 5xx, bad
    credentials, ...): every message would fail, so the whole channel backs off
    and is probed until it recovers.

    ``transient=False`` the channel answered but refused this message: only this
    message backs off and the others keep flowing.

    Messages must never contain the SMS body, a phone number or a secret.
    """

    def __init__(self, message: str, transient: bool = True):
        super().__init__(message)
        self.transient = transient
