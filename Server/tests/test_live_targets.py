"""Targets from real hardware, not the simulator.

The simulator is kind: every node shares one clock with the Server, one hub
carries everything, and packets arrive in order. Real deployments get none of
that. These run the real path — packet bytes, hub stamps, ingest, the tracker's
own loop — with the things real nodes do and the simulator doesn't.
"""
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared" / "echinus-link" / "src"))

import db  # noqa: E402
import ingest  # noqa: E402
import tracker  # noqa: E402
from echinus_link import packets  # noqa: E402
from geometry import enu_to_geodetic, world_to_camera_azel  # noqa: E402

BASE = (37.7749, -122.4194, 0.0)
NODES = {"node-a": (-500.0, 0.0), "node-b": (500.0, 0.0), "node-c": (0.0, 600.0)}
START = np.array([-300.0, 100.0, 400.0])
VELOCITY = np.array([25.0, 5.0, 0.0])
TICK_MS = 200


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    for node_id, (east, north) in NODES.items():
        lat, lon, alt = enu_to_geodetic(np.array([east, north, 0.0]), *BASE)
        db.create_node(connection, node_id, {
            "lat": lat, "lon": lon, "alt_m": alt,
            "yaw_deg": 0.0, "pitch_deg": 90.0, "roll_deg": 0.0, "range_m": 5000.0,
        })
    yield connection
    connection.close()


def transmit(conn, target, node_time_ms, hubs=("hub-1",)):
    """Every node sees `target` and sends a real DETECT packet, which each hub
    in earshot decodes, stamps and forwards — as echinus_hub does."""
    server_now = int(time.time() * 1000)
    for node_id, (east, north) in NODES.items():
        direction = target - np.array([east, north, 0.0])
        az, el = world_to_camera_azel(direction / np.linalg.norm(direction), 0.0, 90.0, 0.0)
        wire = packets.encode_detect(node_id, node_time_ms, az, el)
        for hub in hubs:
            message = packets.decode(wire)
            message["hub_id"] = hub
            message["received_ms"] = server_now
            ingest.record(conn, message)


def fly_live(conn, seconds, clock_offset_ms=0, hubs=("hub-1",), watermark=0, pending=(), t0_ms=0):
    """Fly the target for `seconds`, stepping the tracker's loop once a second."""
    node_epoch = int(time.time() * 1000) + clock_offset_ms
    pending = list(pending)
    for i in range(int(seconds * 1000 / TICK_MS)):
        t_ms = t0_ms + i * TICK_MS
        transmit(conn, START + VELOCITY * (t_ms / 1000.0), node_epoch + t_ms, hubs)
        if i % 5 == 4:
            watermark, pending = tracker.step(conn, watermark, pending)
    return tracker.step(conn, watermark, pending)


@pytest.mark.parametrize("offset_s", [-60, 0, 60])
def test_node_clocks_far_off_the_servers_still_make_a_target(conn, offset_s):
    """NTP drift, or LoRa and relay delay: the nodes agree with each other but
    not with the Server. Closing tracks by comparing the two clocks would end
    every track the moment it began."""
    fly_live(conn, seconds=6, clock_offset_ms=offset_s * 1000)

    [target] = db.list_targets(conn)
    assert target["status"] == "active"
    assert target["contact_count"] == len(db.list_contacts(conn, limit=1000))
    assert target["speed_mps"] == pytest.approx(np.linalg.norm(VELOCITY), rel=0.15)


def test_two_hubs_hearing_every_packet_make_one_target_not_two(conn):
    fly_live(conn, seconds=6, hubs=("hub-1", "hub-2"))

    assert len(db.list_targets(conn)) == 1
    contacts = db.list_contacts(conn, limit=1000)
    times = [c["node_time_ms"] for c in contacts]
    assert len(times) == len(set(times))  # one contact per instant, not a copy per hub


def test_a_late_bucket_joins_the_path_without_wrecking_the_track(conn):
    watermark, pending = fly_live(conn, seconds=6)
    [before] = db.list_targets(conn)

    # A second's worth of packets, one second old, turning up after newer ones.
    node_epoch = before["last_ms"] - 6000 + TICK_MS
    late = 4000
    transmit(conn, START + VELOCITY * (late / 1000.0) + np.array([0, 0, 0.0]), node_epoch + late + 7)
    transmit(conn, START + VELOCITY * 5.9, before["last_ms"] + TICK_MS)  # a newer one, to close the late bucket
    tracker.step(conn, watermark, pending)

    [after] = db.list_targets(conn)
    assert after["id"] == before["id"]
    assert after["contact_count"] >= before["contact_count"] + 1
    assert after["speed_mps"] == pytest.approx(np.linalg.norm(VELOCITY), rel=0.15)
    path = db.track_contacts(conn, after["id"])
    assert [c["node_time_ms"] for c in path] == sorted(c["node_time_ms"] for c in path)


def test_a_target_that_goes_quiet_is_lost_on_the_servers_clock(conn):
    watermark, pending = fly_live(conn, seconds=3, clock_offset_ms=-60_000)
    assert db.list_targets(conn)[0]["status"] == "active"

    conn.execute("UPDATE tracks SET updated_at = datetime('now', '-10 seconds')")
    tracker.step(conn, watermark, pending)
    assert db.list_targets(conn)[0]["status"] == "lost"


def test_the_dashboard_endpoints_serve_live_targets(conn, monkeypatch, tmp_path):
    """What the dashboard polls, against targets made from real packets."""
    from fastapi.testclient import TestClient
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))  # app connects on import
    import app

    monkeypatch.setattr(app, "conn", conn)
    fly_live(conn, seconds=4, clock_offset_ms=-60_000, hubs=("hub-1", "hub-2"))
    client = TestClient(app.app)

    [target] = client.get("/api/targets").json()
    assert target["number"] == 1 and target["status"] == "active"
    assert {"id", "number", "status", "updated_at", "contact_count", "node_ids",
            "lat", "lon", "alt_m", "speed_mps"} <= target.keys()
    path = client.get(f"/api/targets/{target['id']}/contacts").json()
    assert len(path) == target["contact_count"]
    assert all(c["track_id"] == target["id"] for c in path)
    assert client.get("/api/targets/999999/contacts").status_code == 404
