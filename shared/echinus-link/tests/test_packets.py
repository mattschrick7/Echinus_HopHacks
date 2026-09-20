import struct

import pytest

from echinus_link import packets
from echinus_link.packets import Target


def one(target_id=1, az=-12.5, el=31.0, az_rate=4.25, el_rate=-1.5, coasting=False):
    return Target(target_id, az, el, az_rate, el_rate, coasting)


def test_targets_round_trip():
    raw = packets.encode_targets("n01", 7, 250, [one()])
    assert len(raw) == packets.TARGETS_HEAD_SIZE + packets.TARGET_SIZE + packets.CRC_SIZE

    out = packets.decode(raw)
    assert out["type"] == "targets"
    assert out["node_id"] == "n01"
    assert out["seq"] == 7
    assert out["age_ms"] == 250

    (t,) = out["targets"]
    assert t["target_id"] == 1
    assert t["az_deg"] == pytest.approx(-12.5)
    assert t["el_deg"] == pytest.approx(31.0)
    assert t["az_rate_dps"] == pytest.approx(4.25)
    assert t["el_rate_dps"] == pytest.approx(-1.5)
    assert t["confirmed"] is True
    assert t["coasting"] is False


def test_several_targets_ride_in_one_packet():
    targets = [one(1, 1.0, 2.0), one(2, -3.0, 4.0, coasting=True), one(3, 5.5, -6.5)]
    out = packets.decode(packets.encode_targets("n02", 0, 0, targets))

    assert [t["target_id"] for t in out["targets"]] == [1, 2, 3]
    assert [t["coasting"] for t in out["targets"]] == [False, True, False]


def test_heartbeat_round_trip():
    out = packets.decode(packets.encode_heartbeat("n01", 3, 3600))
    assert out == {"type": "heartbeat", "node_id": "n01", "seq": 3, "uptime_s": 3600}


def test_sequence_number_wraps_rather_than_overflowing():
    out = packets.decode(packets.encode_heartbeat("n01", 258, 1))
    assert out["seq"] == 2


def test_long_node_id_is_rejected_not_truncated():
    # Truncating would let "node-01" and "node-02" become the same node.
    with pytest.raises(ValueError):
        packets.encode_heartbeat("node-01", 0, 0)


def test_absurd_and_nan_rates_saturate_instead_of_crashing():
    out = packets.decode(
        packets.encode_targets("n01", 0, 0, [one(az=1e9, el=float("nan"))])
    )
    (t,) = out["targets"]
    assert t["az_deg"] == pytest.approx(327.67)
    assert t["el_deg"] == 0.0


def test_decode_rejects_junk():
    with pytest.raises(ValueError):
        packets.decode(b"hello world")


# ── framing ──────────────────────────────────────────────────────────────────


def test_extract_splits_a_burst_with_header_noise():
    a = packets.encode_targets("n1", 1, 1, [one()])
    b = packets.encode_heartbeat("n2", 2, 7)
    found, leftover = packets.extract(b"\x00\xff" + a + b)

    assert found == [a, b]
    assert leftover == b""


def test_extract_holds_back_a_split_packet():
    a = packets.encode_targets("n1", 1, 1, [one()])
    found, leftover = packets.extract(a[:10])

    assert found == []
    assert leftover == a[:10]  # prepended to the next read, then decodes cleanly

    found, leftover = packets.extract(leftover + a[10:])
    assert found == [a]
    assert leftover == b""


def test_a_flipped_bit_is_dropped_rather_than_delivered():
    a = bytearray(packets.encode_targets("n1", 1, 1, [one()]))
    a[8] ^= 0x01  # somewhere in the payload

    stats = packets.FramingStats()
    found, _ = packets.extract(bytes(a), stats)

    assert found == []
    assert stats.crc_errors == 1


def test_magic_inside_a_payload_does_not_fabricate_a_packet():
    # 0xE5 followed by a valid type byte and a plausible count, buried in an
    # otherwise meaningless stream: the case that used to invent a detection
    # with a garbage node id. It survives as far as the CRC, and dies there.
    noise = bytearray(b"\x00" * 40)
    noise[0] = packets.MAGIC
    noise[1] = packets.TARGETS
    noise[packets.TARGETS_HEAD_SIZE - 1] = 1  # count: one target

    stats = packets.FramingStats()
    found, leftover = packets.extract(bytes(noise), stats)

    assert found == []
    assert stats.crc_errors == 1
    assert len(leftover) <= packets.MAX_PACKET_SIZE


def test_an_impossible_target_count_is_refused_before_the_crc():
    # A count of zero can't size a packet, so it is rejected as a header rather
    # than read and checksummed.
    noise = bytes([packets.MAGIC, packets.TARGETS]) + b"\x00" * 40
    stats = packets.FramingStats()
    found, _ = packets.extract(noise, stats)

    assert found == []
    assert stats.crc_errors == 0
    assert stats.skipped_bytes == len(noise)


def test_a_corrupt_count_byte_cannot_size_a_read():
    a = bytearray(packets.encode_targets("n1", 1, 1, [one()]))
    a[packets.TARGETS_HEAD_SIZE - 1] = 0xFF  # claim 255 targets

    assert packets.packet_size(bytes(a), 0) == 0
    found, _ = packets.extract(bytes(a))
    assert found == []


def test_a_real_packet_after_damage_is_still_found():
    # The reason a CRC failure advances one byte and not a whole packet: the
    # length may be what got corrupted, and a real packet can start inside it.
    damaged = bytearray(packets.encode_targets("n1", 1, 1, [one()]))
    damaged[6] ^= 0xFF
    good = packets.encode_heartbeat("n2", 2, 5)

    found, leftover = packets.extract(bytes(damaged) + good)
    assert found == [good]
    assert leftover == b""


def test_leftover_never_grows_without_bound():
    # A stream of bare MAGIC bytes must not accumulate: each one fails to be a
    # header and is skipped.
    found, leftover = packets.extract(bytes([packets.MAGIC]) * 500)
    assert found == []
    assert len(leftover) <= packets.MAX_PACKET_SIZE


# ── age, not time ────────────────────────────────────────────────────────────


def test_age_round_trips_exactly():
    for age in (0, 1, 250, 5_000, 65_000):
        assert packets.decode(packets.encode_targets("n01", 0, age, [one()]))["age_ms"] == age


def test_an_implausible_age_saturates_rather_than_overflowing():
    # A packet stuck behind a congested channel for over a minute describes
    # something the Server's tracks have long since given up on. Clamping
    # keeps it honest; wrapping would date it to the near past and make a
    # stale bearing look fresh.
    out = packets.decode(packets.encode_targets("n01", 0, 500_000, [one()]))
    assert out["age_ms"] == packets.MAX_AGE_MS


def test_a_negative_age_cannot_be_encoded():
    # Clock nonsense on the node must not become a bearing from the future.
    assert packets.decode(packets.encode_targets("n01", 0, -5, [one()]))["age_ms"] == 0


def test_crc_is_the_documented_ccitt_false():
    # Guards against a future "tidy-up" swapping in a different CRC and
    # silently breaking compatibility with deployed nodes.
    body = packets.encode_heartbeat("n01", 0, 0)[: -packets.CRC_SIZE]
    assert struct.unpack("!H", packets.encode_heartbeat("n01", 0, 0)[-2:])[0] == packets._crc(body)
    assert packets._crc(b"123456789") == 0x29B1
