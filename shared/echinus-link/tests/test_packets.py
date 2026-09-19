import pytest

from echinus_link import packets


def test_detect_round_trip():
    raw = packets.encode_detect("node-01", 1_700_000_000_123, -12.5, 31.0)
    assert len(raw) == packets.DETECT_SIZE

    out = packets.decode(raw)
    assert out["type"] == "detect"
    assert out["node_id"] == "node-01"
    assert out["timestamp_ms"] == 1_700_000_000_123
    assert out["az_deg"] == pytest.approx(-12.5)
    assert out["el_deg"] == pytest.approx(31.0)


def test_heartbeat_round_trip():
    out = packets.decode(packets.encode_heartbeat("node-01", 3600))
    assert out == {"type": "heartbeat", "node_id": "node-01", "uptime_s": 3600}


def test_long_node_id_is_truncated_not_rejected():
    out = packets.decode(packets.encode_detect("a-very-long-node-id", 0, 0.0, 0.0))
    assert out["node_id"] == "a-very-long-"


def test_decode_rejects_junk():
    with pytest.raises(ValueError):
        packets.decode(b"hello world")


def test_extract_splits_a_burst_with_header_noise():
    a = packets.encode_detect("n1", 1, 1.0, 2.0)
    b = packets.encode_heartbeat("n2", 7)
    found, leftover = packets.extract(b"\x00\xff" + a + b)

    assert found == [a, b]
    assert leftover == b""


def test_extract_holds_back_a_split_packet():
    a = packets.encode_detect("n1", 1, 1.0, 2.0)
    found, leftover = packets.extract(a[:10])

    assert found == []
    assert leftover == a[:10]  # prepended to the next read, then decodes cleanly

    found, leftover = packets.extract(leftover + a[10:])
    assert found == [a]
    assert leftover == b""
