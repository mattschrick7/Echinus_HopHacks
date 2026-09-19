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
