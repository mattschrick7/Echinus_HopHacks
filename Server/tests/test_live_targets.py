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
NODES = {"na": (-500.0, 0.0), "nb": (500.0, 0.0), "nc": (0.0, 600.0)}
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


_seq = {}
LATENCY_MS = 250  # detection to hub: jitter, listen-before-talk, airtime


def transmit(conn, target, observed_ms, hubs=("hub-1",), latency_ms=LATENCY_MS):
    """Every node sees `target` and sends a real TARGETS packet, which each hub
    in earshot decodes, expands, stamps and forwards — as echinus_hub does.

    `observed_ms` is when the nodes saw it. Nothing on the wire says so: the
    packet carries only how long ago that was, and each hub turns it back into
    a time using its own arrival clock. Node clocks do not appear anywhere in
    this function, which is the whole point of the arrangement."""
    for node_id, (east, north) in NODES.items():
        # A dict lets one node be slow while another is quick, which is what
        # jitter and a deferred transmission actually do.
        late = latency_ms[node_id] if isinstance(latency_ms, dict) else latency_ms
        seq = _seq[node_id] = (_seq.get(node_id, 0) + 1) & 0xFF
        direction = target - np.array([east, north, 0.0])
        az, el = world_to_camera_azel(direction / np.linalg.norm(direction), 0.0, 90.0, 0.0)
        wire = packets.encode_targets(node_id, seq, late, [packets.Target(1, az, el)])
        for i, hub in enumerate(hubs):
            decoded = packets.decode(wire)
            one = decoded["targets"][0]
            # Each hub stamps its own arrival, a few ms apart — which is why
            # deduplication keys on the packet's identity, not its timestamp.
            arrival = observed_ms + late + i
            ingest.record(conn, {
                "type": "detect",
                "node_id": decoded["node_id"],
                "timestamp_ms": arrival - decoded["age_ms"],
                "az_deg": one["az_deg"],
                "el_deg": one["el_deg"],
                "target_id": one["target_id"],
                "seq": decoded["seq"],
                "hub_id": hub,
                "received_ms": arrival,
            })


def fly_live(conn, seconds, hubs=("hub-1",), watermark=0, pending=(), t0_ms=0,
             latency_ms=LATENCY_MS):
    """Fly the target for `seconds`, stepping the tracker's loop once a second."""
    epoch = int(time.time() * 1000)
    pending = list(pending)
    for i in range(int(seconds * 1000 / TICK_MS)):
        t_ms = t0_ms + i * TICK_MS
        transmit(conn, START + VELOCITY * (t_ms / 1000.0), epoch + t_ms, hubs, latency_ms)
        if i % 5 == 4:
            watermark, pending = tracker.step(conn, watermark, pending)
    return tracker.step(conn, watermark, pending)


@pytest.mark.parametrize("latency_ms", [50, 250, 1800])
def test_a_slow_link_still_makes_a_target(conn, latency_ms):
    """Jitter and listen-before-talk can hold a transmission for up to two
    seconds. Because a packet is dated by its age rather than its arrival,
    that delay shifts the whole timeline and never distorts it."""
    fly_live(conn, seconds=6, latency_ms=latency_ms)

    [target] = db.list_targets(conn)
    assert target["status"] == "active"
    assert target["contact_count"] == len(db.list_contacts(conn, limit=1000))
    assert target["speed_mps"] == pytest.approx(np.linalg.norm(VELOCITY), rel=0.15)


def test_nodes_with_very_different_latencies_still_agree_on_the_moment(conn):
    """The sharp edge of dating packets by age.

    One node's transmission goes out immediately; another's sits nearly two
    seconds behind a busy channel. They saw the same drone at the same instant.
    Dated by arrival they would be nine correlation buckets apart and would
    never triangulate; dated by age they land together."""
    uneven = {"na": 30, "nb": 1_900, "nc": 400}
    observed = int(time.time() * 1000)
    transmit(conn, START, observed, latency_ms=uneven)

    times = {d["node_time_ms"] for d in db.detections_after(conn, 0)}
    assert max(times) - min(times) < tracker.BUCKET_MS

    assert tracker.process(conn, db.detections_after(conn, 0)) == 1
    assert db.list_contacts(conn)[0]["node_ids"] == ["na", "nb", "nc"]


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
    watermark, pending = fly_live(conn, seconds=3)
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
    fly_live(conn, seconds=4, hubs=("hub-1", "hub-2"))
    client = TestClient(app.app)

    [target] = client.get("/api/targets").json()
    assert target["number"] == 1 and target["status"] == "active"
    assert {"id", "number", "status", "updated_at", "contact_count", "node_ids",
            "lat", "lon", "alt_m", "speed_mps"} <= target.keys()
    path = client.get(f"/api/targets/{target['id']}/contacts").json()
    assert len(path) == target["contact_count"]
    assert all(c["track_id"] == target["id"] for c in path)
    assert client.get("/api/targets/999999/contacts").status_code == 404
