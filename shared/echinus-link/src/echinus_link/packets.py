"""
Echinus LoRa wire format — the only bytes that ever travel over the radio.

Shared by the Node (which sends) and the Hub (which receives and relays).
Everything else — where a node is, which way it points, what its detections
mean in the world — lives on the Server. A packet therefore carries only what
the node can know by itself: who it is, when it looked, and where in its own
camera's view the things it is confident about were heading.

Two packet types:

    TARGETS    one or more *confirmed* streaks: a bearing and an angular rate
               each, relative to the lens axis
    HEARTBEAT  "I'm alive" — so the Server can show a node as online even on a
               quiet night with nothing moving

Layout (big-endian):

    TARGETS    MAGIC(1) TYPE(1) node_id(4) seq(1) age_ms(2) n(1)
               then n × [ id(1) flags(1) az(2) el(2) az_rate(2) el_rate(2) ]
               then CRC(2)                          = 12 + 10n bytes
    HEARTBEAT  MAGIC(1) TYPE(1) node_id(4) seq(1) uptime_s(4) CRC(2)  = 13 B

Three things here are worth more than the bytes they cost:

  * **The CRC.** The radio hands us a byte stream, not packets, and extract()
    finds packet starts by scanning for MAGIC. Without a check, a burst that
    lost its framing to a collision resynchronises on the first 0xE5 inside
    some other packet's payload — which happens about once per 256 bytes — and
    fabricates a detection with a garbage node id and a garbage bearing. That
    garbage node id reaches the Server and creates a node. Filtering at the
    node makes collisions rarer; only the CRC makes a mangled packet
    *detectable*.

  * **The sequence number.** One byte, wrapping. It is the only way anyone can
    tell a dropped packet from a quiet camera, and therefore the only way to
    measure whether any of the rest of this is working.

  * **The rates.** A node transmits a *line fit* over its last several frames,
    not a single frame's centroid. Because the rate travels with the bearing,
    the Server can evaluate where the target was at any instant in between,
    which is what lets a node transmit about once a second instead of once a
    frame. See Node/src/echinus_node/streaks.py.

**A packet carries no clock reading at all.** It carries `age_ms`: how long ago
the node saw what it is describing, measured at the instant the bytes go to the
UART. The Hub dates it `received_ms - age_ms`, on the one clock in the system
that everything is compared against.

That is deliberate, and it is not the obvious choice. The obvious choice —
stamping the node's own wall clock — makes every bearing depend on every node's
clock agreeing to within the Server's 200ms correlation bucket, which means
NTP on every Pi and a whole class of failure where two nodes see the same drone
and the Server never pairs them. The other obvious choice, dating a packet by
when it arrived, is worse still: transmissions are deliberately jittered and
deferred by listen-before-talk, so the delay between seeing and arriving is
both large and *variable*, and two nodes observing the same instant can arrive
seconds apart.

Age is immune to both. It is measured after the jitter and the deferral, so
the only error left is airtime — which is near enough constant, and the same
for every node, so it shifts them all together and correlation never notices.
"""
from __future__ import annotations

import binascii
import struct
from dataclasses import dataclass
from typing import NamedTuple

MAGIC = 0xE5  # first byte of every packet; lets a receiver find packet starts

# Type bytes are deliberately not 0x01/0x02. Those meant fixed-size packets of
# a different shape, and a node still running that build must fail to decode
# here rather than half-decode into plausible nonsense.
TARGETS = 0x11
HEARTBEAT = 0x12

NODE_ID_BYTES = 4  # node ids are up to 4 ASCII characters

# What a single packet may claim to hold. extract() checks the count byte
# against this before trusting it to compute a length.
MAX_TARGETS = 8

# Angles and angular rates travel as int16 hundredths: 0.01° of resolution over
# a ±327° range. The detector is nowhere near that precise — the simulator
# models 0.1° of noise — and the camera's field of view is ±31°.
ANGLE_SCALE = 100.0

# Observation age in milliseconds, unsigned 16-bit: a little over a minute.
# Anything older than this has been queued behind a congested channel for so
# long that the Server's tracks have already timed out, so saturating is the
# honest answer.
MAX_AGE_MS = 0xFFFF

_TARGETS_HEAD_FMT = "!BB4sBHB"   # magic, type, node id, seq, age_ms, count
_TARGET_FMT = "!BBhhhh"          # id, flags, az, el, az_rate, el_rate
_HEARTBEAT_BODY_FMT = "!BB4sBI"  # magic, type, node id, seq, uptime
_CRC_FMT = "!H"

TARGETS_HEAD_SIZE = struct.calcsize(_TARGETS_HEAD_FMT)      # 10
TARGET_SIZE = struct.calcsize(_TARGET_FMT)                  # 10
HEARTBEAT_BODY_SIZE = struct.calcsize(_HEARTBEAT_BODY_FMT)  # 11
CRC_SIZE = struct.calcsize(_CRC_FMT)                        # 2

HEARTBEAT_SIZE = HEARTBEAT_BODY_SIZE + CRC_SIZE             # 13
MAX_PACKET_SIZE = TARGETS_HEAD_SIZE + MAX_TARGETS * TARGET_SIZE + CRC_SIZE

# flags bits
_FLAG_CONFIRMED = 0x01
_FLAG_COASTING = 0x02  # no blob in the most recent frame; the fit is coasting



class Target(NamedTuple):
    """One confirmed streak, as the node sees it off its own lens axis.

    az is positive to the right of centre, el positive above it; the rates are
    the line fit's slopes, in degrees per second. `target_id` is local to the
    node and stable for as long as the streak lives — it is what lets the Hub
    and Server see a continuing target rather than unrelated blobs.
    """

    target_id: int
    az_deg: float
    el_deg: float
    az_rate_dps: float = 0.0
    el_rate_dps: float = 0.0
    coasting: bool = False


@dataclass
class FramingStats:
    """What extract() threw away, for the Hub to report.

    Both of these are silent in a working deployment, so a non-zero count is
    always worth surfacing: crc_errors means packets are being damaged in the
    air, skipped_bytes means the stream is carrying something that isn't ours.
    """

    crc_errors: int = 0
    skipped_bytes: int = 0


def _pack_id(node_id: str) -> bytes:
    """Node id as a fixed-width, null-padded field.

    Unlike the 12-byte field this replaced, an over-long id is an error rather
    than something to truncate: at four characters, silently trimming would let
    two nodes in one deployment collapse onto the same id.
    """
    raw = node_id.encode("ascii", "ignore")
    if not raw:
        raise ValueError("node_id must not be empty")
    if len(raw) > NODE_ID_BYTES:
        raise ValueError(
            f"node_id {node_id!r} is longer than {NODE_ID_BYTES} ASCII characters"
        )
    return raw.ljust(NODE_ID_BYTES, b"\x00")


def _unpack_id(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("ascii", "ignore")


def _quantise(value: float) -> int:
    """Degrees (or degrees/second) to int16 hundredths, saturating.

    Saturating rather than raising, and mapping NaN to zero, because the input
    is a least-squares fit: a degenerate streak can produce an absurd slope or
    a NaN, and a node that crashes on one is worse than a node that reports a
    clipped one the Server will discard anyway.
    """
    if value != value:  # NaN
        return 0
    return max(-32768, min(32767, int(round(value * ANGLE_SCALE))))


def _crc(data: bytes) -> int:
    """CRC-16/CCITT-FALSE, as the stdlib happens to provide it."""
    return binascii.crc_hqx(data, 0xFFFF)


def encode_targets(
    node_id: str, seq: int, age_ms: int, targets: list[Target]
) -> bytes:
    """One packet carrying every target the node currently wants to report.

    `age_ms` is how long ago these bearings were observed, and it must be
    computed at the moment of transmission rather than when the packet was
    built — everything between those two instants (the jitter, the queue, the
    module deferring for a busy channel) is exactly the delay this field
    exists to cancel. See Node's Transmitter, which calls this from inside the
    worker thread for that reason.

    Batching matters: the radio's per-transmission overhead (preamble, header,
    the module's own framing) is paid once here however many targets ride
    along, and it dwarfs the 10 bytes each one costs.
    """
    if not 1 <= len(targets) <= MAX_TARGETS:
        raise ValueError(f"a packet carries 1-{MAX_TARGETS} targets, not {len(targets)}")

    body = struct.pack(
        _TARGETS_HEAD_FMT,
        MAGIC,
        TARGETS,
        _pack_id(node_id),
        seq & 0xFF,
        max(0, min(MAX_AGE_MS, int(age_ms))),
        len(targets),
    )
    for t in targets:
        flags = _FLAG_CONFIRMED | (_FLAG_COASTING if t.coasting else 0)
        body += struct.pack(
            _TARGET_FMT,
            t.target_id & 0xFF,
            flags,
            _quantise(t.az_deg),
            _quantise(t.el_deg),
            _quantise(t.az_rate_dps),
            _quantise(t.el_rate_dps),
        )
    return body + struct.pack(_CRC_FMT, _crc(body))


def encode_heartbeat(node_id: str, seq: int, uptime_s: int) -> bytes:
    body = struct.pack(
        _HEARTBEAT_BODY_FMT, MAGIC, HEARTBEAT, _pack_id(node_id), seq & 0xFF, uptime_s
    )
    return body + struct.pack(_CRC_FMT, _crc(body))


def packet_size(buf: bytes, offset: int) -> int | None:
    """How long the packet starting at `offset` claims to be.

    Returns None when `buf` doesn't yet hold enough to tell, and 0 when what is
    there cannot be a packet header at all. Kept separate from extract() so the
    length arithmetic — the part an attacker or a corrupt byte would aim at —
    is testable on its own.
    """
    if offset + 1 >= len(buf):
        return None  # trailing MAGIC; its type byte hasn't arrived yet
    kind = buf[offset + 1]

    if kind == HEARTBEAT:
        return HEARTBEAT_SIZE
    if kind != TARGETS:
        return 0

    count_at = offset + TARGETS_HEAD_SIZE - 1
    if count_at >= len(buf):
        return None  # the count byte itself is still in flight
    count = buf[count_at]
    if not 1 <= count <= MAX_TARGETS:
        return 0  # a corrupt count byte must never size a read
    return TARGETS_HEAD_SIZE + count * TARGET_SIZE + CRC_SIZE


def decode(data: bytes) -> dict:
    """One packet's bytes -> a plain dict. Raises ValueError on anything else.

    `age_ms` comes back as it travelled. Turning it into a time is the Hub's
    job, because the Hub is the thing that knows when the packet arrived:
    `timestamp_ms = received_ms - age_ms`.
    """
    if len(data) < 2 or data[0] != MAGIC:
        raise ValueError("not an Echinus packet")

    size = packet_size(data, 0)
    if not size or len(data) < size:
        raise ValueError(f"unknown or incomplete packet type 0x{data[1]:02x}")
    if _crc(data[: size - CRC_SIZE]) != struct.unpack(_CRC_FMT, data[size - CRC_SIZE : size])[0]:
        raise ValueError("CRC mismatch")

    kind = data[1]
    if kind == HEARTBEAT:
        _, _, nid, seq, uptime = struct.unpack(_HEARTBEAT_BODY_FMT, data[:HEARTBEAT_BODY_SIZE])
        return {
            "type": "heartbeat",
            "node_id": _unpack_id(nid),
            "seq": seq,
            "uptime_s": uptime,
        }

    _, _, nid, seq, age_ms, count = struct.unpack(_TARGETS_HEAD_FMT, data[:TARGETS_HEAD_SIZE])
    targets = []
    for i in range(count):
        at = TARGETS_HEAD_SIZE + i * TARGET_SIZE
        tid, flags, az, el, az_rate, el_rate = struct.unpack(
            _TARGET_FMT, data[at : at + TARGET_SIZE]
        )
        targets.append({
            "target_id": tid,
            "az_deg": az / ANGLE_SCALE,
            "el_deg": el / ANGLE_SCALE,
            "az_rate_dps": az_rate / ANGLE_SCALE,
            "el_rate_dps": el_rate / ANGLE_SCALE,
            "confirmed": bool(flags & _FLAG_CONFIRMED),
            "coasting": bool(flags & _FLAG_COASTING),
        })
    return {
        "type": "targets",
        "node_id": _unpack_id(nid),
        "seq": seq,
        "age_ms": age_ms,
        "targets": targets,
    }


def extract(buf: bytes, stats: FramingStats | None = None) -> tuple[list[bytes], bytes]:
    """Pull whole, CRC-clean packets out of a byte stream.

    The LoRa HAT hands us UART bursts, not packets: one read can contain the
    radio's own address header, several packets back to back, or a packet cut
    off halfway. Scan for MAGIC, slice out each complete packet, and return
    (packets, leftover). Feed the leftover back in front of the next read.

    A packet whose CRC fails advances the scan by a single byte rather than by
    its claimed length. That matters: if the length was what got corrupted,
    skipping it would step over a real packet that starts inside the range.

    Leftover can never exceed MAX_PACKET_SIZE, because the only way to leave
    bytes behind is to stop short of one packet's worth.
    """
    packets: list[bytes] = []
    i = 0
    while i < len(buf):
        if buf[i] != MAGIC:
            i += 1
            if stats:
                stats.skipped_bytes += 1
            continue

        size = packet_size(buf, i)
        if size is None:
            break  # can't know the length yet; wait for more bytes
        if size == 0:
            i += 1
            if stats:
                stats.skipped_bytes += 1
            continue
        if len(buf) - i < size:
            break  # partial packet — wait for the rest

        packet = buf[i : i + size]
        expected = struct.unpack(_CRC_FMT, packet[-CRC_SIZE:])[0]
        if _crc(packet[:-CRC_SIZE]) != expected:
            i += 1
            if stats:
                stats.crc_errors += 1
            continue

        packets.append(packet)
        i += size
    return packets, buf[i:]
