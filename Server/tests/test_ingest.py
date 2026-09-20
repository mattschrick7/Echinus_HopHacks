"""The real data path: packet bytes -> hub message -> ingest -> tracker."""
import sys
from pathlib import Path

import numpy as np
import pytest

# The wire format lives in the shared package; the Server image doesn't install
# it, so reach for its source to build packets exactly as a node does.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "echinus-link" / "src"))

import db  # noqa: E402
import ingest  # noqa: E402
import tracker  # noqa: E402
from echinus_link import packets  # noqa: E402
from geometry import enu_to_geodetic, geodetic_to_enu, world_to_camera_azel  # noqa: E402

BASE = (37.7749, -122.4194, 0.0)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def place(conn, node_id, east, north, yaw, pitch):
    lat, lon, alt = enu_to_geodetic(np.array([east, north, 0.0]), *BASE)
    db.create_node(conn, node_id, {"lat": lat, "lon": lon, "alt_m": alt,
                                   "yaw_deg": yaw, "pitch_deg": pitch, "roll_deg": 0.0})
    return np.array([east, north, 0.0])


def over_the_air(node_id, timestamp_ms, az, el, received_ms=None, seq=0):
    """Exactly what a hub forwards: the node's packet, decoded and expanded
    into one detection per target, plus the hub's stamps.

    Mirrors echinus_hub.relay.Relay._expand. Note what the node does *not*
    send: a clock reading. `timestamp_ms` here is the instant we want the
    detection to land on, and it is expressed on the wire as an age relative
    to the hub's arrival time — which is the only clock in play."""
    arrival = received_ms if received_ms is not None else timestamp_ms
    wire = packets.encode_targets(
        node_id, seq, arrival - timestamp_ms, [packets.Target(1, az, el)]
    )
    decoded = packets.decode(wire)
    target = decoded["targets"][0]

    message = {
        "type": "detect",
        "node_id": decoded["node_id"],
        "timestamp_ms": arrival - decoded["age_ms"],
        "az_deg": target["az_deg"],
        "el_deg": target["el_deg"],
        "target_id": target["target_id"],
        "seq": decoded["seq"],
        "hub_id": "hub-1",
    }
    if received_ms is not None:
        message["received_ms"] = received_ms
    return message


def test_real_packets_from_two_nodes_become_a_contact(conn):
    """Two real-format detections, nodes facing each other, same instant."""
    target = np.array([0.0, 150.0, 250.0])
    nodes = {"na": place(conn, "na", -500.0, 0.0, 90.0, 30.0),
             "nb": place(conn, "nb", 500.0, 0.0, 270.0, 30.0)}
    for node_id, position in nodes.items():
        n = db.get_node(conn, node_id)
        direction = (target - position) / np.linalg.norm(target - position)
        az, el = world_to_camera_azel(direction, n["yaw_deg"], n["pitch_deg"], n["roll_deg"])
        ingest.record(conn, over_the_air(node_id, 1_000_000, az, el, received_ms=1_000_150))

    assert tracker.process(conn, db.detections_after(conn, 0)) == 1
    contact = db.list_contacts(conn)[0]
    fix = geodetic_to_enu(contact["lat"], contact["lon"], contact["alt_m"], *BASE)
    # Quantising the angles to hundredths of a degree costs a little
    # precision; still well inside a metre at this range.
    assert np.linalg.norm(fix - target) < 1.0
    assert contact["node_ids"] == ["na", "nb"]


def test_the_stored_offset_measures_the_link_not_the_clock(conn):
    """What this column means changed when nodes stopped sending a time.

    It used to be clock skew — how far a node's clock had drifted from the
    hub's. Nodes now send no clock reading at all, only how long ago they saw
    something, so the figure is transport delay: jitter, waiting for a clear
    channel, and airtime. It is always negative, and a large magnitude means
    the channel is congested rather than that a Pi needs NTP."""
    place(conn, "na", 0.0, 0.0, 0.0, 30.0)
    ingest.record(conn, over_the_air("na", 1_000_000, 0.0, 0.0, received_ms=1_000_200, seq=1))
    assert db.get_node(conn, "na")["clock_offset_ms"] == pytest.approx(-200)

    # A node stuck behind a busy channel drags the smoothed figure out.
    for i in range(60):
        ingest.record(conn, over_the_air(
            "na", 1_000_000 + i, 0.0, 0.0, received_ms=1_001_800 + i, seq=i + 2))
    assert db.get_node(conn, "na")["clock_offset_ms"] < -1_500


def test_simulated_messages_leave_the_clock_unknown(conn):
    """The simulator shares one clock and sends no hub arrival time."""
    place(conn, "sim-0", 0.0, 0.0, 0.0, 30.0)
    ingest.record(conn, {"type": "detect", "node_id": "sim-0", "timestamp_ms": 1, "az_deg": 0.0, "el_deg": 0.0})
    assert db.get_node(conn, "sim-0")["clock_offset_ms"] is None


def test_an_angle_outside_the_set_view_is_flagged(conn):
    place(conn, "na", 0.0, 0.0, 0.0, 30.0)  # 62.2 x 48.8 by default
    ingest.record(conn, over_the_air("na", 1, 30.0, 20.0))
    assert db.get_node(conn, "na")["out_of_view_at"] is None

    # A node whose node.toml says 90 degrees across reports 40 degrees off-axis.
    ingest.record(conn, over_the_air("na", 2, 40.0, 5.0))
    node = db.get_node(conn, "na")
    assert node["out_of_view_at"] is not None
    assert "az 40.0" in node["out_of_view_note"]

    # Correcting the field of view in the dashboard clears the warning.
    db.update_node(conn, "na", {"fov_h_deg": 90.0})
    assert db.get_node(conn, "na")["out_of_view_at"] is None


def test_the_same_packet_through_two_hubs_is_stored_once(conn):
    place(conn, "na", 0.0, 0.0, 0.0, 30.0)
    # Two hubs, one packet. Each dates it by its own arrival, so the two copies
    # no longer share a timestamp — the sequence number is what identifies them
    # as the same transmission.
    for offset, hub in enumerate(("hub-1", "hub-2")):
        message = over_the_air("na", 1_000_000, 3.5, -2.0, received_ms=1_000_100 + offset, seq=7)
        message["hub_id"] = hub
        ingest.record(conn, message)
    assert len(db.list_detections(conn)) == 1

    # The node's next transmission is a different packet, even at the same
    # bearing — a target it is still tracking reports the same angle twice.
    ingest.record(conn, over_the_air("na", 1_000_200, 3.5, -2.0, received_ms=1_000_300, seq=8))
    assert len(db.list_detections(conn)) == 2
