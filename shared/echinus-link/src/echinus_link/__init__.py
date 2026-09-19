"""Echinus radio link — the wire format and the LoRa HAT driver.

Shared by the Node (sends) and the Hub (receives). Import `Radio` only where a
HAT is actually present; `packets` is pure Python and safe to import anywhere.
"""
from echinus_link.packets import (
    DETECT,
    HEARTBEAT,
    decode,
    encode_detect,
    encode_heartbeat,
    extract,
)

__all__ = [
    "DETECT",
    "HEARTBEAT",
    "decode",
    "encode_detect",
    "encode_heartbeat",
    "extract",
]
