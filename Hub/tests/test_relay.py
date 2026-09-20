"""The hub's two real jobs: expanding packets, and noticing what went missing."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "echinus-link" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from echinus_hub.relay import Relay, Stats  # noqa: E402
from echinus_link import packets  # noqa: E402
from echinus_link.packets import Target  # noqa: E402


@pytest.fixture
def relay():
    return Relay(radio=None, server_url="ws://unused", hub_id="hub-1")


# ── loss accounting ──────────────────────────────────────────────────────────


def test_a_clean_run_loses_nothing():
    stats = Stats()
    for seq in range(1, 20):
        assert stats.saw("n01", seq) == 0
    assert stats.lost == {}
    assert stats.received["n01"] == 19


def test_a_gap_is_counted_as_loss():
    stats = Stats()
    stats.saw("n01", 5)
    assert stats.saw("n01", 9) == 3  # 6, 7, 8 never arrived
    assert stats.lost["n01"] == 3


def test_loss_is_counted_per_node():
    stats = Stats()
    stats.saw("n01", 1)
    stats.saw("n02", 1)
    stats.saw("n01", 4)  # two lost
    assert stats.saw("n02", 2) == 0  # n02 is fine
    assert stats.lost == {"n01": 2}


def test_a_gap_across_the_sequence_wrap_is_still_a_small_number():
    stats = Stats()
    stats.saw("n01", 254)
    assert stats.saw("n01", 2) == 3  # 255, 0, 1
    assert stats.lost["n01"] == 3


def test_a_node_that_restarts_is_one_loss_not_two_hundred():
    # A restarted node's counter jumps arbitrarily. Counting that literally
    # would swamp the number this exists to report, so a gap too big to be
    # plausible loss is recorded as a single event.
    stats = Stats()
    stats.saw("n01", 200)
    stats.saw("n01", 100)  # a jump of 155: a restart, not 155 lost packets
    assert stats.lost["n01"] == 1


def test_a_plausible_gap_is_still_counted_in_full():
    # The flip side: a one-byte counter genuinely cannot tell "62 lost" from
    # "194 sent while we weren't listening", so the line has to be drawn
    # somewhere. Below it, believe the gap.
    stats = Stats()
    stats.saw("n01", 200)
    stats.saw("n01", 7)  # a jump of 62, which is survivable packet loss
    assert stats.lost["n01"] == 62


def test_the_first_packet_from_a_node_is_never_a_loss():
    stats = Stats()
    assert stats.saw("n01", 137) == 0
    assert stats.lost == {}


def test_the_summary_reports_every_node_and_the_framing():
    stats = Stats()
    stats.saw("n01", 1)
    stats.saw("n01", 3)
    stats.framing.crc_errors = 2
    line = stats.summary()

    assert "n01: 2 rx, 1 lost" in line
    assert "2 CRC fail" in line


# ── expanding packets ────────────────────────────────────────────────────────


def test_one_packet_of_two_targets_becomes_two_detections(relay):
    wire = packets.encode_targets("n01", 1, 200, [
        Target(1, -12.5, 3.0, 8.0, 1.5),
        Target(2, 20.0, -4.0, -6.0, 0.5, coasting=True),
    ])
    messages = relay._expand(packets.decode(wire))

    assert [m["type"] for m in messages] == ["detect", "detect"]
    assert [m["target_id"] for m in messages] == [1, 2]
    assert messages[0]["az_deg"] == pytest.approx(-12.5)
    assert messages[0]["az_rate_dps"] == pytest.approx(8.0)
    assert messages[1]["coasting"] is True
    assert all(m["hub_id"] == "hub-1" for m in messages)
    assert all("received_ms" in m for m in messages)


def test_the_hub_dates_a_detection_from_its_own_clock(relay):
    # The node sends no time at all, only how stale its bearing is.
    before = int(time.time() * 1000)
    wire = packets.encode_targets("n01", 1, 400, [Target(1, 0.0, 0.0)])
    (message,) = relay._expand(packets.decode(wire))
    after = int(time.time() * 1000)

    assert before - 400 <= message["timestamp_ms"] <= after - 400


def test_a_nodes_own_clock_never_reaches_the_server(relay):
    """The point of the whole arrangement.

    Two nodes whose clocks disagree by an hour, reporting bearings they saw at
    the same moment, must still land in the same correlation bucket — because
    neither of their clocks is consulted. Under the old wire format, where
    nodes stamped their own time, these two would have been an hour apart and
    the Server would never have paired them.
    """
    a = relay._expand(packets.decode(packets.encode_targets("n01", 1, 300, [Target(1, 1.0, 2.0)])))
    b = relay._expand(packets.decode(packets.encode_targets("n02", 1, 300, [Target(1, 3.0, 4.0)])))

    assert abs(a[0]["timestamp_ms"] - b[0]["timestamp_ms"]) < 200  # same bucket


def test_a_heartbeat_becomes_one_message_and_still_counts_for_loss(relay):
    relay._expand(packets.decode(packets.encode_heartbeat("n01", 1, 10)))
    (message,) = relay._expand(packets.decode(packets.encode_heartbeat("n01", 4, 70)))

    assert message["type"] == "heartbeat"
    assert message["uptime_s"] == 70
    assert relay.stats.lost["n01"] == 2  # heartbeats share the sequence counter


def test_a_full_burst_of_two_nodes_round_trips(relay):
    """What the hub actually reads off the UART: several packets, back to back,
    behind the sender header the module leaves on the air."""
    burst = b"\x00\x00\x41"  # the three header bytes that travel as payload
    burst += packets.encode_targets("n01", 1, 150, [Target(1, 1.0, 2.0)])
    burst += packets.encode_heartbeat("n02", 1, 60)
    burst += packets.encode_targets("n01", 2, 150, [Target(1, 2.0, 2.5)])

    found, leftover = packets.extract(burst, relay.stats.framing)
    messages = [m for raw in found for m in relay._expand(packets.decode(raw))]

    assert leftover == b""
    assert [m["node_id"] for m in messages] == ["n01", "n02", "n01"]
    assert relay.stats.lost == {}
    assert relay.stats.framing.crc_errors == 0
