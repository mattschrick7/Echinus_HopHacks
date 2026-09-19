"""
All the coordinate maths, in one place.

Nodes report where something is *in their own camera view*. This module turns
that into a bearing in the world, using the position and orientation the
operator entered on this Server — which is why all of it lives here and none
of it lives on a node.

Frames
------
Geodetic   latitude/longitude in degrees, altitude in metres.
ENU        local flat plane in metres around a reference point, axes
           (East, North, Up).
World az/el  az is compass bearing, clockwise from North (0 = N, 90 = E).
             el is the angle above the horizon (90 = straight up).
Camera az/el  what a node sends: degrees off its own lens axis,
              +az to the right, +el upward.

Node orientation, as entered by the operator:
    yaw_deg    compass bearing the lens points (0 = North, 90 = East)
    pitch_deg  how far above the horizon the lens points (90 = straight up)
    roll_deg   rotation of the camera about the lens axis, clockwise as seen
               from behind the camera (0 for a level camera)

Geodetic<->ENU is a flat-earth approximation around the reference point. Over
the tens of kilometres a deployment spans it is far more accurate than the
az/el noise, and the forward and inverse are exact inverses of each other.
"""
from __future__ import annotations

import math

import numpy as np

METRES_PER_DEGREE = 111_320.0  # mean metres per degree of latitude


# ── geodetic <-> ENU ─────────────────────────────────────────────────────────

def geodetic_to_enu(lat, lon, alt, ref_lat, ref_lon, ref_alt) -> np.ndarray:
    """(lat, lon, alt) -> ENU metres relative to the reference point."""
    east = (lon - ref_lon) * METRES_PER_DEGREE * math.cos(math.radians(ref_lat))
    north = (lat - ref_lat) * METRES_PER_DEGREE
    return np.array([east, north, alt - ref_alt], dtype=float)


def enu_to_geodetic(enu, ref_lat, ref_lon, ref_alt) -> tuple[float, float, float]:
    """ENU metres -> (lat, lon, alt). Exact inverse of geodetic_to_enu."""
    east, north, up = (float(v) for v in enu)
    lat = ref_lat + north / METRES_PER_DEGREE
    lon = ref_lon + east / (METRES_PER_DEGREE * math.cos(math.radians(ref_lat)))
    return lat, lon, ref_alt + up


def mean_position(points) -> tuple[float, float, float]:
    """Average of (lat, lon, alt) triples — used as the shared ENU origin."""
    points = list(points)
    if not points:
        raise ValueError("need at least one point")
    n = len(points)
    return tuple(sum(p[i] for p in points) / n for i in range(3))


# ── world bearings ───────────────────────────────────────────────────────────

def azel_to_unit(az_deg: float, el_deg: float) -> np.ndarray:
    """World (az, el) -> unit vector in ENU."""
    az, el = math.radians(az_deg), math.radians(el_deg)
    horizontal = math.cos(el)
    return np.array([horizontal * math.sin(az), horizontal * math.cos(az), math.sin(el)])


def unit_to_azel(vector) -> tuple[float, float]:
    """Any non-zero ENU vector -> world (az in [0, 360), el)."""
    east, north, up = np.asarray(vector, dtype=float) / np.linalg.norm(vector)
    az = math.degrees(math.atan2(east, north)) % 360.0
    el = math.degrees(math.asin(max(-1.0, min(1.0, up))))
    return az, el


def camera_axes(yaw_deg: float, pitch_deg: float, roll_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The camera's forward / right / up directions, expressed in ENU.

    Build the un-rolled frame first — forward from yaw and pitch, right
    horizontal and 90 degrees clockwise from it, up completing the set — then
    spin right and up around forward by the roll angle.
    """
    yaw, pitch, roll = map(math.radians, (yaw_deg, pitch_deg, roll_deg))

    forward = np.array([
        math.sin(yaw) * math.cos(pitch),
        math.cos(yaw) * math.cos(pitch),
        math.sin(pitch),
    ])
    right = np.array([math.cos(yaw), -math.sin(yaw), 0.0])
    up = np.cross(right, forward)

    if roll:
        right, up = (
            right * math.cos(roll) + up * math.sin(roll),
            up * math.cos(roll) - right * math.sin(roll),
        )
    return forward, right, up


def camera_to_world_azel(
    cam_az_deg: float,
    cam_el_deg: float,
    yaw_deg: float,
    pitch_deg: float,
    roll_deg: float = 0.0,
) -> tuple[float, float]:
    """A node's camera-relative (az, el) -> a world bearing (az, el).

    This is the one function that consumes operator-entered orientation. The
    node's az/el are pinhole angles — tan(az) and tan(el) are the offsets from
    the lens axis in focal lengths — which is exactly how the detector
    produced them, so the two stay consistent.
    """
    forward, right, up = camera_axes(yaw_deg, pitch_deg, roll_deg)
    direction = (
        forward
        + right * math.tan(math.radians(cam_az_deg))
        + up * math.tan(math.radians(cam_el_deg))
    )
    return unit_to_azel(direction)


def world_to_camera_azel(
    direction,
    yaw_deg: float,
    pitch_deg: float,
    roll_deg: float = 0.0,
) -> tuple[float, float] | None:
    """The inverse: an ENU direction -> what the camera would report.

    Returns None if the direction is behind the camera. Used by the simulator
    and by the round-trip test that pins this convention down.
    """
    forward, right, up = camera_axes(yaw_deg, pitch_deg, roll_deg)
    along = float(direction @ forward)
    if along <= 0:
        return None
    return (
        math.degrees(math.atan2(float(direction @ right), along)),
        math.degrees(math.atan2(float(direction @ up), along)),
    )


# ── ray intersection ─────────────────────────────────────────────────────────

def closest_approach(p1, d1, p2, d2):
    """Where two rays (point + direction) come nearest to each other.

    Returns (midpoint, gap_metres, t1, t2), where t1/t2 are distances along
    each ray — negative means the point is *behind* that camera. Returns None
    when the rays are parallel and there's no unique answer.
    """
    r = p1 - p2
    a, b, c = float(d1 @ d1), float(d1 @ d2), float(d2 @ d2)
    d, e = float(d1 @ r), float(d2 @ r)

    denominator = a * c - b * b
    if abs(denominator) < 1e-9:
        return None

    t1 = (b * e - c * d) / denominator
    t2 = (a * e - b * d) / denominator
    point1, point2 = p1 + t1 * d1, p2 + t2 * d2
    return (point1 + point2) / 2.0, float(np.linalg.norm(point1 - point2)), t1, t2
