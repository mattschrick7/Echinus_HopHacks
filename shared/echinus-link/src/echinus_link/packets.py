"""
Echinus LoRa wire format — the only bytes that ever travel over the radio.

Shared by the Node (which sends) and the Hub (which receives and relays).
Everything else — where a node is, which way it points, what its detections
mean in the world — lives on the Server. A packet therefore carries only what
the node can know by itself: who it is, when it saw something, and where in its
own camera's view it saw it.

Two packet types:

    DETECT     the camera saw motion at (az, el) *relative to the lens axis*
    HEARTBEAT  "I'm alive" — so the Server can show a node as online even on a
               quiet night with no detections

Layout (big-endian, fixed size per type):

    DETECT     MAGIC(1) TYPE(1) node_id(12) timestamp_ms(8) az(4) el(4)  = 30 B
    HEARTBEAT  MAGIC(1) TYPE(1) node_id(12) uptime_s(4)                  = 18 B

Both are far under LoRa's 240-byte payload limit.
"""
from __future__ import annotations

import struct

MAGIC = 0xE5  # first byte of every packet; lets a receiver find packet starts

DETECT = 0x01
HEARTBEAT = 0x02

NODE_ID_BYTES = 12  # node ids are up to 12 ASCII characters

_DETECT_FMT = "!BB12sQff"
_HEARTBEAT_FMT = "!BB12sI"

DETECT_SIZE = struct.calcsize(_DETECT_FMT)        # 30
HEARTBEAT_SIZE = struct.calcsize(_HEARTBEAT_FMT)  # 18

PACKET_SIZES = {DETECT: DETECT_SIZE, HEARTBEAT: HEARTBEAT_SIZE}


def _pack_id(node_id: str) -> bytes:
    """Node id as a fixed-width, null-padded field."""
    raw = node_id.encode("ascii", "ignore")
    if not raw:
        raise ValueError("node_id must not be empty")
    return raw[:NODE_ID_BYTES].ljust(NODE_ID_BYTES, b"\x00")


def _unpack_id(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("ascii", "ignore")


def encode_detect(node_id: str, timestamp_ms: int, az_deg: float, el_deg: float) -> bytes:
    """az/el are offsets from the camera's own lens axis, in degrees.

    az: positive to the right of centre.  el: positive above centre.
    The Server turns these into world bearings using the node's operator-set
    position and orientation.
    """
    return struct.pack(_DETECT_FMT, MAGIC, DETECT, _pack_id(node_id), timestamp_ms, az_deg, el_deg)


def encode_heartbeat(node_id: str, uptime_s: int) -> bytes:
    return struct.pack(_HEARTBEAT_FMT, MAGIC, HEARTBEAT, _pack_id(node_id), uptime_s)


def decode(data: bytes) -> dict:
    """One packet's bytes -> a plain dict. Raises ValueError on anything else.

    The dict is exactly what the Hub forwards to the Server as JSON, so this
    function defines the websocket message shape too.
    """
    if len(data) < 2 or data[0] != MAGIC:
        raise ValueError("not an Echinus packet")

    kind = data[1]
    if kind == DETECT:
        _, _, nid, ts, az, el = struct.unpack(_DETECT_FMT, data[:DETECT_SIZE])
        return {
            "type": "detect",
            "node_id": _unpack_id(nid),
            "timestamp_ms": ts,
            "az_deg": az,
            "el_deg": el,
        }
    if kind == HEARTBEAT:
        _, _, nid, uptime = struct.unpack(_HEARTBEAT_FMT, data[:HEARTBEAT_SIZE])
        return {"type": "heartbeat", "node_id": _unpack_id(nid), "uptime_s": uptime}

    raise ValueError(f"unknown packet type 0x{kind:02x}")


def extract(buf: bytes) -> tuple[list[bytes], bytes]:
    """Pull whole packets out of a byte stream.

    The LoRa HAT hands us UART bursts, not packets: one read can contain the
    radio's own address header, several packets back to back, or a packet cut
    off halfway. Scan for MAGIC, slice out each complete packet, and return
    (packets, leftover). Feed the leftover back in front of the next read.
    """
    packets: list[bytes] = []
    i = 0
    while i < len(buf):
        if buf[i] != MAGIC:
            i += 1
            continue
        if i + 1 >= len(buf):
            break  # trailing MAGIC — its type byte hasn't arrived yet
        size = PACKET_SIZES.get(buf[i + 1])
        if size is None:
            i += 1
            continue
        if len(buf) - i < size:
            break  # partial packet — wait for the rest
        packets.append(buf[i:i + size])
        i += size
    return packets, buf[i:]
