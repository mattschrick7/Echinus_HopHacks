import numpy as np
import pytest

from geometry import (
    azel_to_unit,
    camera_axes,
    camera_to_world_azel,
    closest_approach,
    enu_to_geodetic,
    geodetic_to_enu,
    unit_to_azel,
    world_to_camera_azel,
)


def test_enu_round_trip():
    lat, lon, alt = 37.79, -122.40, 120.0
    enu = geodetic_to_enu(lat, lon, alt, 37.7749, -122.4194, 50.0)
    back = enu_to_geodetic(enu, 37.7749, -122.4194, 50.0)

    assert back == pytest.approx((lat, lon, alt))


def test_enu_axes_point_the_right_way():
    # A point due north and above the reference.
    enu = geodetic_to_enu(38.0, -122.0, 100.0, 37.0, -122.0, 0.0)
    assert enu[0] == pytest.approx(0.0)      # no easting
    assert enu[1] > 0                        # northing
    assert enu[2] == pytest.approx(100.0)    # up


def test_azel_conventions():
    assert unit_to_azel(azel_to_unit(0, 0)) == pytest.approx((0.0, 0.0))       # north
    assert unit_to_azel(azel_to_unit(90, 0)) == pytest.approx((90.0, 0.0))     # east
    assert azel_to_unit(0, 90) == pytest.approx([0, 0, 1])                     # straight up


def test_camera_axes_are_orthonormal():
    forward, right, up = camera_axes(37.0, 22.0, 15.0)

    for vector in (forward, right, up):
        assert np.linalg.norm(vector) == pytest.approx(1.0)
    assert forward @ right == pytest.approx(0.0, abs=1e-12)
    assert forward @ up == pytest.approx(0.0, abs=1e-12)


def test_level_camera_facing_east():
    """Centre of frame is the lens axis; +az is to the right of it."""
    az, el = camera_to_world_azel(0.0, 0.0, yaw_deg=90.0, pitch_deg=0.0)
    assert (az, el) == pytest.approx((90.0, 0.0))

    az, _ = camera_to_world_azel(10.0, 0.0, yaw_deg=90.0, pitch_deg=0.0)
    assert az == pytest.approx(100.0)  # right of east is south-east


def test_camera_pointing_at_the_zenith():
    """A node staring straight up, yawed north.

    Tilting a camera up rotates the top of its frame backwards over the
    operator's head, so at the zenith the top of the frame faces south — the
    exact case that goes wrong if orientation is applied in the wrong order.
    """
    assert camera_to_world_azel(0.0, 0.0, yaw_deg=0.0, pitch_deg=90.0)[1] == pytest.approx(90.0)

    az, el = camera_to_world_azel(0.0, 10.0, yaw_deg=0.0, pitch_deg=90.0)
    assert el == pytest.approx(80.0)
    assert az == pytest.approx(180.0)


@pytest.mark.parametrize("orientation", [(0, 0, 0), (90, 0, 0), (37, 22, 15), (200, 75, -40)])
@pytest.mark.parametrize("camera_angles", [(0, 0), (12, -5), (-25, 20)])
def test_camera_world_round_trip(orientation, camera_angles):
    """The two directions of the conversion must agree, whatever the mounting."""
    yaw, pitch, roll = orientation
    az, el = camera_to_world_azel(*camera_angles, yaw, pitch, roll)
    back = world_to_camera_azel(azel_to_unit(az, el), yaw, pitch, roll)

    assert back == pytest.approx(camera_angles)


def test_rays_that_cross():
    """Two nodes 1 km apart, both looking at a point above the midpoint."""
    target = np.array([500.0, 0.0, 1000.0])
    a, b = np.array([0.0, 0.0, 0.0]), np.array([1000.0, 0.0, 0.0])

    midpoint, gap, t_a, t_b = closest_approach(
        a, (target - a) / np.linalg.norm(target - a),
        b, (target - b) / np.linalg.norm(target - b),
    )

    assert midpoint == pytest.approx(target)
    assert gap == pytest.approx(0.0, abs=1e-6)
    assert t_a > 0 and t_b > 0  # in front of both cameras


def test_rays_pointing_away_give_negative_distance():
    """Looking in opposite directions: the maths still solves, but behind them."""
    result = closest_approach(
        np.array([0.0, 0.0, 0.0]), azel_to_unit(180, 10),
        np.array([1000.0, 0.0, 0.0]), azel_to_unit(0, 10),
    )
    assert result is not None
    _, _, t_a, t_b = result
    assert t_a < 0 or t_b < 0  # the tracker rejects these


def test_parallel_rays_have_no_crossing():
    direction = azel_to_unit(45, 30)
    assert closest_approach(np.zeros(3), direction, np.array([100.0, 0.0, 0.0]), direction) is None


# ── field-of-view footprint ──────────────────────────────────────────────────

from geometry import geodetic_to_enu as _to_enu, view_footprint  # noqa: E402

_BASE = (37.7749, -122.4194)


def _outline(**aim):
    """The footprint as ENU (east, north) metres around the node, drawn to 600 m."""
    outline = view_footprint(*_BASE, range_m=600.0, **aim)
    return np.array([_to_enu(lat, lon, 0.0, *_BASE, 0.0)[:2] for lat, lon in outline])


def test_level_camera_draws_a_wedge_opening_along_its_yaw():
    outline = _outline(yaw_deg=90.0, pitch_deg=0.0)
    assert np.allclose(outline.min(axis=0)[0], 0.0, atol=1e-6)  # starts at the node...
    assert outline[:, 0].max() > 500.0                          # ...and runs east
    half_width = outline[:, 1].max()
    assert 300.0 < half_width < 330.0  # 600 m * sin(31.1 deg), give or take the corners


def test_camera_pointing_up_draws_a_patch_around_the_node():
    outline = _outline(yaw_deg=0.0, pitch_deg=90.0)
    assert outline[:, 0].min() < -100 and outline[:, 0].max() > 100
    assert outline[:, 1].min() < -100 and outline[:, 1].max() > 100


def test_tilting_up_shortens_the_cone():
    reach = lambda pitch: _outline(yaw_deg=0.0, pitch_deg=pitch)[:, 1].max()
    # Past half the vertical field, the near edge climbs and the cone pulls in.
    assert reach(40.0) > reach(65.0) > reach(85.0)


def test_roll_turns_the_footprint():
    level = _outline(yaw_deg=0.0, pitch_deg=90.0, roll_deg=0.0)
    rolled = _outline(yaw_deg=0.0, pitch_deg=90.0, roll_deg=30.0)
    assert not np.allclose(level.max(axis=0), rolled.max(axis=0), atol=1.0)
