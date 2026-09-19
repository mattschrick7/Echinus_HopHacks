"""End-to-end check of the bit that matters: bearings in, positions out."""
import numpy as np
import pytest

import db
import tracker
from geometry import enu_to_geodetic, geodetic_to_enu, unit_to_azel

BASE = (37.7749, -122.4194, 0.0)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def place_node(conn, node_id, east, north):
    """A node `east`/`north` metres from BASE, looking straight up."""
    lat, lon, alt = enu_to_geodetic(np.array([east, north, 0.0]), *BASE)
    db.create_node(conn, node_id, {
        "lat": lat, "lon": lon, "alt_m": alt,
        "yaw_deg": 0.0, "pitch_deg": 90.0, "roll_deg": 0.0,
    })
    return np.array([east, north, 0.0])


def see(conn, node_id, node_enu, target_enu, time_ms=1000):
    """Record what a node at `node_enu` sees when a target is at `target_enu`."""
    az, el = unit_to_azel(target_enu - node_enu)
    db.insert_detection(conn, node_id, "test-hub", time_ms, 0.0, 0.0, world=(az, el))


def test_two_nodes_seeing_one_target_produce_one_contact(conn):
    target = np.array([100.0, 50.0, 2000.0])
    a = place_node(conn, "node-a", -500.0, 0.0)
    b = place_node(conn, "node-b", 500.0, 0.0)

    see(conn, "node-a", a, target)
    see(conn, "node-b", b, target)

    assert tracker.process(conn, db.detections_after(conn, 0)) == 1

    contact = db.list_contacts(conn)[0]
    assert contact["node_count"] == 2
    fix = geodetic_to_enu(contact["lat"], contact["lon"], contact["alt_m"], *BASE)
    assert np.linalg.norm(fix - target) < 50.0  # within 50 m of the truth


def test_three_nodes_give_one_contact_not_three(conn):
    """Three nodes make three ray pairs — they must merge into a single object."""
    target = np.array([0.0, 0.0, 2500.0])
    nodes = {
        "node-a": place_node(conn, "node-a", -500.0, 0.0),
        "node-b": place_node(conn, "node-b", 500.0, 0.0),
        "node-c": place_node(conn, "node-c", 0.0, 500.0),
    }
    for node_id, position in nodes.items():
        see(conn, node_id, position, target)

    assert tracker.process(conn, db.detections_after(conn, 0)) == 1
    assert db.list_contacts(conn)[0]["node_count"] == 3


def test_one_node_alone_is_not_evidence(conn):
    a = place_node(conn, "node-a", -500.0, 0.0)
    place_node(conn, "node-b", 500.0, 0.0)
    see(conn, "node-a", a, np.array([0.0, 0.0, 2000.0]))

    assert tracker.process(conn, db.detections_after(conn, 0)) == 0


def test_rays_that_miss_each_other_are_rejected(conn):
    """Two nodes seeing unrelated things must not invent a contact between them."""
    a = place_node(conn, "node-a", -500.0, 0.0)
    b = place_node(conn, "node-b", 500.0, 0.0)

    see(conn, "node-a", a, np.array([-3000.0, 2000.0, 1500.0]))
    see(conn, "node-b", b, np.array([3000.0, -2000.0, 6000.0]))

    assert tracker.process(conn, db.detections_after(conn, 0)) == 0


def test_detections_far_apart_in_time_are_not_paired(conn):
    target = np.array([0.0, 0.0, 2000.0])
    a = place_node(conn, "node-a", -500.0, 0.0)
    b = place_node(conn, "node-b", 500.0, 0.0)

    see(conn, "node-a", a, target, time_ms=1000)
    see(conn, "node-b", b, target, time_ms=1000 + 10 * tracker.BUCKET_MS)

    assert tracker.process(conn, db.detections_after(conn, 0)) == 0


def test_saving_an_empty_form_does_not_place_a_node_at_zero(conn):
    """0,0 is the row's default and a real spot in the Atlantic — not a position."""
    db.ensure_node(conn, "node-07")

    saved = db.update_node(conn, "node-07", {"name": "typo", "lat": 0.0, "lon": 0.0})
    assert saved["configured"] == 0

    saved = db.update_node(conn, "node-07", {"lat": 37.78, "lon": -122.42})
    assert saved["configured"] == 1

    # And clearing it back out un-configures the node rather than stranding it.
    assert db.update_node(conn, "node-07", {"lat": 0.0, "lon": 0.0})["configured"] == 0


def test_unconfigured_nodes_are_ignored(conn):
    """A node that has transmitted but hasn't been placed yet can't contribute."""
    target = np.array([0.0, 0.0, 2000.0])
    a = place_node(conn, "node-a", -500.0, 0.0)
    b = place_node(conn, "node-b", 500.0, 0.0)
    db.ensure_node(conn, "node-new")  # seen on the radio, never positioned

    see(conn, "node-a", a, target)
    see(conn, "node-b", b, target)

    assert db.get_node(conn, "node-new")["configured"] == 0
    assert tracker.process(conn, db.detections_after(conn, 0)) == 1


def test_disabled_nodes_are_left_out(conn):
    target = np.array([0.0, 0.0, 2000.0])
    a = place_node(conn, "node-a", -500.0, 0.0)
    b = place_node(conn, "node-b", 500.0, 0.0)
    db.update_node(conn, "node-b", {"enabled": 0})

    see(conn, "node-a", a, target)
    see(conn, "node-b", b, target)

    assert tracker.process(conn, db.detections_after(conn, 0)) == 0


def test_contacts_remember_which_nodes_saw_them(conn):
    target = np.array([100.0, 50.0, 2000.0])
    a = place_node(conn, "node-a", -500.0, 0.0)
    b = place_node(conn, "node-b", 500.0, 0.0)
    see(conn, "node-a", a, target)
    see(conn, "node-b", b, target)
    tracker.process(conn, db.detections_after(conn, 0))

    (contact,) = db.list_contacts(conn)
    assert contact["node_ids"] == ["node-a", "node-b"]
    assert contact["node_count"] == 2
    assert 0.0 <= contact["age_s"] < 5.0


def test_max_age_hides_old_contacts(conn):
    db.insert_contact(conn, 1.0, 2.0, 100.0, ["node-a", "node-b"])
    conn.execute("UPDATE contacts SET observed_at = datetime('now', '-120 seconds')")
    db.insert_contact(conn, 1.0, 2.0, 100.0, ["node-a", "node-b"])

    assert len(db.list_contacts(conn)) == 2
    assert len(db.list_contacts(conn, max_age_s=60)) == 1


def test_an_old_database_gains_the_new_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE nodes (node_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE contacts (id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL DEFAULT (datetime('now')),"
        " lat REAL NOT NULL, lon REAL NOT NULL, alt_m REAL, node_count INTEGER NOT NULL);"
        "INSERT INTO contacts (lat, lon, node_count) VALUES (1, 2, 2);"
    )
    old.close()

    upgraded = db.connect(path)
    (contact,) = db.list_contacts(upgraded)
    assert contact["node_ids"] == []
    upgraded.close()


def test_two_objects_at_once_give_two_contacts_not_phantoms(conn):
    """Each node sees both objects. The four rays make four crossings, two of
    them between rays to *different* objects — those must not become contacts,
    nor get averaged into the real ones."""
    first, second = np.array([-150.0, 100.0, 200.0]), np.array([150.0, 60.0, 260.0])
    a = place_node(conn, "node-a", -500.0, 0.0)
    b = place_node(conn, "node-b", 500.0, 0.0)
    for target in (first, second):
        see(conn, "node-a", a, target)
        see(conn, "node-b", b, target)

    assert tracker.process(conn, db.detections_after(conn, 0)) == 2
    for contact in db.list_contacts(conn):
        position = geodetic_to_enu(contact["lat"], contact["lon"], contact["alt_m"], *BASE)
        assert min(np.linalg.norm(position - t) for t in (first, second)) < 1.0


def test_a_ray_is_never_shared_between_contacts(conn):
    """Three nodes, two objects: every node's ray ends up in exactly one contact."""
    first, second = np.array([0.0, 200.0, 250.0]), np.array([100.0, -150.0, 300.0])
    nodes = {
        "node-a": place_node(conn, "node-a", -500.0, 0.0),
        "node-b": place_node(conn, "node-b", 500.0, 0.0),
        "node-c": place_node(conn, "node-c", 0.0, 500.0),
    }
    for node_id, position in nodes.items():
        for target in (first, second):
            see(conn, node_id, position, target)

    assert tracker.process(conn, db.detections_after(conn, 0)) == 2
    assert [c["node_count"] for c in db.list_contacts(conn)] == [3, 3]
