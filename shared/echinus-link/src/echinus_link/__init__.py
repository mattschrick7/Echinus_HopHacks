"""Echinus radio link — the wire format and the LoRa HAT driver.

Shared by the Node (sends) and the Hub (receives). Import `Radio` only where a
HAT is actually present; `packets` is pure Python and safe to import anywhere.
"""
from echinus_link.packets import (
    HEARTBEAT,
    TARGETS,
    FramingStats,
    Target,
    decode,
    encode_heartbeat,
    encode_targets,
    extract,
)

__all__ = [
    "HEARTBEAT",
    "TARGETS",
    "FramingStats",
    "Target",
    "decode",
    "encode_heartbeat",
    "encode_targets",
    "extract",
]
