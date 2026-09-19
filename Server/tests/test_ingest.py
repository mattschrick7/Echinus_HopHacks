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


def over_the_air(node_id, timestamp_ms, az, el, received_ms=None):
    """Exactly what a hub forwards: the node's packet, decoded, plus the hub's stamps."""
    message = packets.decode(packets.encode_detect(node_id, timestamp_ms, az, el))
    message["hub_id"] = "hub-1"
    if received_ms is not None:
        message["received_ms"] = received_ms
    return message


def test_real_packets_from_two_nodes_become_a_contact(conn):
    """Two real-format detections, nodes facing each other, same instant."""
    target = np.array([0.0, 150.0, 250.0])
    nodes = {"node-a": place(conn, "node-a", -500.0, 0.0, 90.0, 30.0),
             "node-b": place(conn, "node-b", 500.0, 0.0, 270.0, 30.0)}
    for node_id, position in nodes.items():
        n = db.get_node(conn, node_id)
        direction = (target - position) / np.linalg.norm(target - position)
        az, el = world_to_camera_azel(direction, n["yaw_deg"], n["pitch_deg"], n["roll_deg"])
        ingest.record(conn, over_the_air(node_id, 1_000_000, az, el, received_ms=1_000_150))

    assert tracker.process(conn, db.detections_after(conn, 0)) == 1
    contact = db.list_contacts(conn)[0]
    fix = geodetic_to_enu(contact["lat"], contact["lon"], contact["alt_m"], *BASE)
    # Float32 on the radio costs a little precision; still well inside a metre.
    assert np.linalg.norm(fix - target) < 1.0
    assert contact["node_ids"] == ["node-a", "node-b"]


def test_clock_offset_is_measured_from_hub_arrival(conn):
    place(conn, "node-a", 0.0, 0.0, 0.0, 30.0)
    ingest.record(conn, over_the_air("node-a", 1_000_000, 0.0, 0.0, received_ms=1_000_200))
    assert db.get_node(conn, "node-a")["clock_offset_ms"] == pytest.approx(-200)

    # A clock that has wandered 30 s off drags the smoothed figure after it.
    for i in range(60):
        ingest.record(conn, over_the_air("node-a", 1_030_000 + i, 0.0, 0.0, received_ms=1_000_000 + i))
    assert db.get_node(conn, "node-a")["clock_offset_ms"] > 25_000


def test_simulated_messages_leave_the_clock_unknown(conn):
    """The simulator shares one clock and sends no hub arrival time."""
    place(conn, "sim-0", 0.0, 0.0, 0.0, 30.0)
    ingest.record(conn, {"type": "detect", "node_id": "sim-0", "timestamp_ms": 1, "az_deg": 0.0, "el_deg": 0.0})
    assert db.get_node(conn, "sim-0")["clock_offset_ms"] is None


def test_an_angle_outside_the_set_view_is_flagged(conn):
    place(conn, "node-a", 0.0, 0.0, 0.0, 30.0)  # 62.2 x 48.8 by default
    ingest.record(conn, over_the_air("node-a", 1, 30.0, 20.0))
    assert db.get_node(conn, "node-a")["out_of_view_at"] is None

    # A node whose node.toml says 90 degrees across reports 40 degrees off-axis.
    ingest.record(conn, over_the_air("node-a", 2, 40.0, 5.0))
    node = db.get_node(conn, "node-a")
    assert node["out_of_view_at"] is not None
    assert "az 40.0" in node["out_of_view_note"]

    # Correcting the field of view in the dashboard clears the warning.
    db.update_node(conn, "node-a", {"fov_h_deg": 90.0})
    assert db.get_node(conn, "node-a")["out_of_view_at"] is None


def test_the_same_packet_through_two_hubs_is_stored_once(conn):
    place(conn, "node-a", 0.0, 0.0, 0.0, 30.0)
    for hub in ("hub-1", "hub-2"):
        message = over_the_air("node-a", 1_000_000, 3.5, -2.0, received_ms=1_000_100)
        message["hub_id"] = hub
        ingest.record(conn, message)
    ingest.record(conn, over_the_air("node-a", 1_000_200, 3.5, -2.0))  # a new moment: kept
    assert len(db.list_detections(conn)) == 2
