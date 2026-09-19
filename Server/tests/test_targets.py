"""Chaining contacts into targets: the same drone should keep the same id."""
import random
from collections import Counter, defaultdict

import numpy as np
import pytest

import db
import ingest
import simulator
import targets
import tracker
from geometry import geodetic_to_enu, unit_to_azel

BASE = (37.7749, -122.4194, 0.0)
STEP_MS = 200  # one tracker bucket


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def place_node(conn, node_id, east, north):
    from geometry import enu_to_geodetic
    lat, lon, alt = enu_to_geodetic(np.array([east, north, 0.0]), *BASE)
    db.create_node(conn, node_id, {
        "lat": lat, "lon": lon, "alt_m": alt,
        "yaw_deg": 0.0, "pitch_deg": 90.0, "roll_deg": 0.0, "range_m": 5000.0,
    })
    return np.array([east, north, 0.0])


@pytest.fixture
def nodes(conn):
    return {
        "node-a": place_node(conn, "node-a", -500.0, 0.0),
        "node-b": place_node(conn, "node-b", 500.0, 0.0),
        "node-c": place_node(conn, "node-c", 0.0, 600.0),
    }


def see(conn, nodes, target_enu, time_ms, only=None):
    for node_id, node_enu in nodes.items():
        if only and node_id not in only:
            continue
        az, el = unit_to_azel(target_enu - node_enu)
        db.insert_detection(conn, node_id, "test-hub", time_ms, 0.0, 0.0, world=(az, el))


def fly(conn, nodes, paths, steps, start_ms=0):
    """Each path is (start, velocity); every node sees every path each step."""
    for i in range(steps):
        t = start_ms + i * STEP_MS
        for start, velocity in paths:
            see(conn, nodes, start + velocity * (t / 1000.0), t)


def track_all(conn):
    tracker.process(conn, db.detections_after(conn, 0))
    return db.list_contacts(conn, limit=10_000)


def test_one_drone_keeps_one_target(conn, nodes):
    fly(conn, nodes, [(np.array([-300.0, 100.0, 400.0]), np.array([25.0, 5.0, 0.0]))], steps=20)
    contacts = track_all(conn)

    assert len(contacts) == 20
    assert len({c["track_id"] for c in contacts}) == 1
    [target] = db.list_targets(conn)
    assert target["number"] == 1 and target["status"] == "active"
    assert target["contact_count"] == 20
    assert target["speed_mps"] == pytest.approx(np.hypot(25, 5), rel=0.1)
    assert db.track_contacts(conn, target["id"])[0]["node_time_ms"] == 0


def test_two_drones_at_once_stay_separate(conn, nodes):
    fly(conn, nodes, [
        (np.array([-400.0, -200.0, 300.0]), np.array([30.0, 0.0, 0.0])),
        (np.array([200.0, -400.0, 500.0]), np.array([0.0, 28.0, 0.0])),
    ], steps=25)
    contacts = track_all(conn)

    by_track = defaultdict(list)
    for c in contacts:
        by_track[c["track_id"]].append(c)
    assert len(by_track) == 2
    for group in by_track.values():
        altitudes = {round(c["alt_m"], -2) for c in group}
        assert len(altitudes) == 1  # each track holds only one drone's contacts
    assert sorted(t["number"] for t in db.list_targets(conn)) == [1, 2]


def test_parallel_drones_close_together_stay_separate(conn, nodes):
    velocity = np.array([20.0, 0.0, 0.0])
    fly(conn, nodes, [
        (np.array([-300.0, 0.0, 400.0]), velocity),
        (np.array([-300.0, 150.0, 400.0]), velocity),
    ], steps=25)
    contacts = track_all(conn)

    for track_id in {c["track_id"] for c in contacts}:
        north = [geodetic_to_enu(c["lat"], c["lon"], c["alt_m"], *BASE)[1]
                 for c in contacts if c["track_id"] == track_id]
        assert max(north) - min(north) < 50  # never jumps to the other drone
    assert len(db.list_targets(conn)) == 2


def test_a_long_gap_loses_the_target_and_starts_a_new_one(conn, nodes):
    path = [(np.array([-300.0, 0.0, 400.0]), np.array([20.0, 0.0, 0.0]))]
    fly(conn, nodes, path, steps=10)
    fly(conn, nodes, path, steps=10, start_ms=10 * STEP_MS + targets.LOST_AFTER_MS + 1000)
    track_all(conn)

    old, new = sorted(db.list_targets(conn), key=lambda t: t["number"])
    assert (old["number"], old["status"]) == (1, "lost")
    assert (new["number"], new["status"]) == (2, "active")


def test_lost_targets_are_permanent_and_keep_their_path(conn, nodes):
    path = [(np.array([-300.0, 0.0, 400.0]), np.array([20.0, 0.0, 0.0]))]
    fly(conn, nodes, path, steps=10)
    fly(conn, nodes, path, steps=10, start_ms=10 * STEP_MS + targets.LOST_AFTER_MS + 1000)
    track_all(conn)
    # Pretend the first one was lost a day ago.
    conn.execute("UPDATE tracks SET updated_at = datetime('now', '-1 day') WHERE number = 1")

    listed = db.list_targets(conn)
    assert [(t["number"], t["status"]) for t in listed] == [(2, "active"), (1, "lost")]
    assert len(db.track_contacts(conn, listed[1]["id"])) == 10


def test_a_one_off_contact_never_becomes_a_target(conn, nodes):
    see(conn, nodes, np.array([100.0, 100.0, 300.0]), 0)
    # Later traffic somewhere else entirely, long after.
    fly(conn, nodes, [(np.array([-300.0, -300.0, 600.0]), np.array([20.0, 0.0, 0.0]))],
        steps=5, start_ms=targets.LOST_AFTER_MS + 1000)
    contacts = track_all(conn)

    stray = next(c for c in contacts if c["node_time_ms"] == 0)
    assert db.get_track(conn, stray["track_id"])["status"] == "dropped"
    assert [t["number"] for t in db.list_targets(conn)] == [1]


def test_shared_nodes_help_but_are_not_required(conn, nodes):
    """A drone passing from two cameras' view into a different pair's keeps its id."""
    start, velocity = np.array([-300.0, 100.0, 400.0]), np.array([25.0, 0.0, 0.0])
    for i in range(20):
        t = i * STEP_MS
        only = {"node-a", "node-b"} if i < 10 else {"node-b", "node-c"}
        see(conn, nodes, start + velocity * (t / 1000.0), t, only=only)
    contacts = track_all(conn)

    assert len({c["track_id"] for c in contacts}) == 1


def test_targets_carry_on_across_a_restart(conn, nodes):
    """All state is in the database, so processing in two runs changes nothing."""
    path = [(np.array([-300.0, 0.0, 400.0]), np.array([20.0, 0.0, 0.0]))]
    fly(conn, nodes, path, steps=10)
    tracker.process(conn, db.detections_after(conn, 0))
    watermark = db.latest_detection_id(conn)

    fly(conn, nodes, path, steps=10, start_ms=10 * STEP_MS)
    tracker.process(conn, db.detections_after(conn, watermark))

    assert len({c["track_id"] for c in db.list_contacts(conn, limit=100)}) == 1


def test_expire_on_the_wall_clock_loses_a_silent_target(conn, nodes):
    fly(conn, nodes, [(np.array([0.0, 0.0, 400.0]), np.array([20.0, 0.0, 0.0]))], steps=5)
    track_all(conn)
    targets.expire(conn, 4 * STEP_MS + targets.LOST_AFTER_MS + 1)
    assert db.list_targets(conn)[0]["status"] == "lost"


# ── against the simulator's ground truth ─────────────────────────────────────

def run_simulator(conn, seconds, start_s=0.0, hz=5):
    """Feed the simulator's detections through the real ingest and tracker,
    and return {contact id: the target it really was, or None}."""
    random.seed(1)
    sim_nodes = simulator.build_nodes()
    for n in sim_nodes:
        db.create_node(conn, n["node_id"], {k: n[k] for k in (
            "lat", "lon", "alt_m", "yaw_deg", "pitch_deg", "roll_deg",
            "fov_h_deg", "fov_v_deg", "range_m")})

    truth_at = {}
    for i in range(int(seconds * hz)):
        wall_s = start_s + i / hz
        elapsed = wall_s % simulator.LOOP_S
        t_ms = int(wall_s * 1000)
        truth_at[t_ms] = simulator.target_positions(elapsed)
        for node_id, az, el in simulator.observations(sim_nodes, elapsed):
            ingest.record(conn, {"type": "detect", "node_id": node_id, "timestamp_ms": t_ms,
                                 "az_deg": az, "el_deg": el, "hub_id": "sim-hub"})
    tracker.process(conn, db.detections_after(conn, 0))

    origin = tracker._node_positions(conn)[1]
    ring_centre = geodetic_to_enu(simulator.BASE_LAT, simulator.BASE_LON, 10.0, *origin)
    labels = {}
    for c in db.list_contacts(conn, limit=100_000):
        fix = geodetic_to_enu(c["lat"], c["lon"], c["alt_m"], *origin) - ring_centre
        truth = truth_at[c["node_time_ms"]]
        name, distance = min(((k, np.linalg.norm(fix - p)) for k, p in truth.items()),
                             key=lambda kv: kv[1])
        labels[c["id"]] = (c["track_id"], name if distance < 60 else None)
    return labels


def test_simulated_drones_each_become_one_clean_target(conn):
    labels = run_simulator(conn, seconds=60)
    numbered = {t["id"] for t in db.list_targets(conn)}

    per_track = defaultdict(Counter)
    for track_id, truth in labels.values():
        if track_id in numbered:
            per_track[track_id][truth] += 1

    # Every target is (almost) entirely one real drone…
    for counts in per_track.values():
        top, n = counts.most_common(1)[0]
        assert top is not None
        assert n / sum(counts.values()) >= 0.95

    # …and each real drone is (almost) entirely one target, not split up.
    for drone in ("alpha", "bravo"):
        tracks = Counter(t for t, truth in labels.values() if truth == drone and t in numbered)
        assert tracks, f"{drone} was never tracked"
        assert tracks.most_common(1)[0][1] / sum(tracks.values()) >= 0.9


def test_the_simulator_loop_wrap_starts_new_targets(conn):
    # The drones are in view roughly 34-63 s into each 100 s loop. Run from
    # 50 s to 140 s: seen, gone, the wrap puts them back at the start, seen again.
    run_simulator(conn, seconds=90, start_s=50.0)
    before = [t for t in db.list_targets(conn) if t["last_ms"] < 100_000]
    after = [t for t in db.list_targets(conn) if t["first_ms"] >= 100_000]
    assert after, "no targets after the wrap"
    assert not {t["id"] for t in before} & {t["id"] for t in after}
