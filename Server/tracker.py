"""
Turning bearings into positions.

Each node only gives us a direction. Cross two directions from two different
places and you get a point. This runs continuously in the background of the
Server, reading new detections and writing `contacts`.

Per cycle:
  1. Read detections that arrived since last time (only ones with a world
     bearing, i.e. from a configured node).
  2. Bucket them by time, so we only compare things seen at the same moment.
  3. In each bucket, try every pair of rays from *different* nodes and keep the
     pairs that genuinely meet — within a small angle of each other, in front
     of both cameras, within both nodes' detection range, at a believable
     height. The range matters with real nodes: a node that picks up a distant
     plane sends a bearing like any other, and two such bearings can cross far
     beyond where either camera could really make out a drone. A lone node seeing something is
     not evidence of an object's position.
  4. Hand the rays out to objects, closest-meeting pairs first, and never give
     one ray to two objects. A ray is one node seeing one thing; when two
     objects are in the air together, a ray to the first can pass near a ray to
     the second, and without this rule that near miss becomes a phantom contact
     (or gets averaged into a real one and drags it off). Any other node whose
     ray also passes through the object joins it, so three nodes seeing one
     thing make one contact with node_count = 3.

Each contact is then handed to targets.py, which chains contacts over time
into targets (T-1, T-2, …) and stamps the contact with its track id.

The newest bucket is held back one cycle so a straggling detection has a chance
to join it before the bucket is closed.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import numpy as np

import db
import targets
from geometry import (
    azel_to_unit,
    closest_approach,
    distance_to_ray,
    enu_to_geodetic,
    geodetic_to_enu,
    mean_position,
    nearest_point_to_rays,
)

# Tuning knobs. These are the numbers to reach for when tracking looks wrong.
POLL_S = 1.0                 # how often to look for new detections
BUCKET_MS = 200              # detections this close in time are "simultaneous"
# Two rays count as meeting if they pass within this angle of each other, as
# seen from the cameras: detector noise plus a little slack for an orientation
# typed in by hand. An angle rather than a fixed distance, because a small
# aiming error opens into a big gap far away and only a tiny one close in.
MEETING_ANGLE_DEG = 1.5
MIN_GAP_M = 5.0              # floor, so very close objects aren't held to millimetres
MIN_NODES = 2                # nodes that must agree before a contact is written
MIN_ALT_M, MAX_ALT_M = 0.0, 30_000.0
MAX_RANGE_M = 50_000.0


def _believable(point: np.ndarray) -> bool:
    return MIN_ALT_M <= point[2] <= MAX_ALT_M and np.hypot(point[0], point[1]) <= MAX_RANGE_M


def _tolerance(range_m: float) -> float:
    """How far apart two rays may pass, at this range, and still meet."""
    return max(MIN_GAP_M, range_m * np.tan(np.radians(MEETING_ANGLE_DEG)))


# A ray: (node id, origin in ENU, unit direction, the node's detection range).
Ray = tuple[str, np.ndarray, np.ndarray, float]


def crossings(rays: list[Ray]) -> list[tuple[float, int, int, np.ndarray]]:
    """Every cross-node ray pair that genuinely meets somewhere plausible.

    Returns (gap, ray index, ray index, meeting point), best meetings first.
    """
    found = []
    for i, (node_a, origin_a, direction_a, range_a) in enumerate(rays):
        for j in range(i + 1, len(rays)):
            node_b, origin_b, direction_b, range_b = rays[j]
            if node_a == node_b:
                continue  # a node can't triangulate against itself
            result = closest_approach(origin_a, direction_a, origin_b, direction_b)
            if result is None:
                continue
            midpoint, gap, t_a, t_b = result
            if not (0 < t_a <= range_a and 0 < t_b <= range_b) or not _believable(midpoint):
                continue
            if gap > _tolerance(min(t_a, t_b)):
                continue
            found.append((gap, i, j, midpoint))
    return sorted(found, key=lambda f: f[0])


def associate(rays: list[Ray]) -> list[tuple[np.ndarray, set]]:
    """Group rays into objects, each ray used at most once.

    Seed an object from the best unclaimed pair, then let every other node
    whose unclaimed ray passes through it join, refitting the position as each
    one does. Returns (position, node ids) per object.
    """
    used: set[int] = set()
    objects = []
    for _gap, i, j, midpoint in crossings(rays):
        if i in used or j in used:
            continue  # one of these rays already belongs to another object
        members = [i, j]
        point = midpoint

        # Other nodes' rays that also pass through this object, nearest first.
        candidates = []
        for k, (node_k, origin_k, direction_k, range_k) in enumerate(rays):
            if k in used or k in members:
                continue
            miss, along = distance_to_ray(point, origin_k, direction_k)
            if 0 < along <= range_k and miss <= _tolerance(along):
                candidates.append((miss, k))
        for _miss, k in sorted(candidates):
            if rays[k][0] in {rays[m][0] for m in members}:
                continue  # one ray per node per object
            members.append(k)
            point = nearest_point_to_rays([rays[m][1] for m in members], [rays[m][2] for m in members])

        used.update(members)
        objects.append((point, {rays[m][0] for m in members}))
    return objects


def _node_positions(conn) -> tuple[dict[str, tuple[np.ndarray, float]], tuple[float, float, float]] | None:
    """Configured, enabled nodes as (ENU position, detection range) around
    their shared centre."""
    nodes = [n for n in db.list_nodes(conn) if n["configured"] and n["enabled"]]
    if len(nodes) < MIN_NODES:
        return None
    origin = mean_position((n["lat"], n["lon"], n["alt_m"]) for n in nodes)
    return {
        n["node_id"]: (geodetic_to_enu(n["lat"], n["lon"], n["alt_m"], *origin), n["range_m"])
        for n in nodes
    }, origin


def process(conn, detections: list[dict]) -> int:
    """Triangulate a batch of detections. Returns how many contacts were written."""
    positions = _node_positions(conn)
    if positions is None:
        return 0
    node_enu, origin = positions

    buckets: dict[int, list[dict]] = defaultdict(list)
    for d in detections:
        buckets[d["node_time_ms"] // BUCKET_MS].append(d)

    written = 0
    for key in sorted(buckets):  # oldest first: tracks must see time move forwards
        group = buckets[key]
        rays = []
        for d in group:
            if d["node_id"] not in node_enu:
                continue
            position, range_m = node_enu[d["node_id"]]
            rays.append((d["node_id"], position, azel_to_unit(d["world_az_deg"], d["world_el_deg"]), range_m))
        fixes = [(centre, nodes) for centre, nodes in associate(rays) if len(nodes) >= MIN_NODES]
        if not fixes:
            continue
        # The bucket's moment on the nodes' clock, which is what target
        # velocities are measured against (observed_at is only write time).
        t_ms = round(sum(d["node_time_ms"] for d in group) / len(group))
        track_ids = targets.assign(conn, fixes, t_ms, origin)
        for (centre, nodes), track_id in zip(fixes, track_ids):
            lat, lon, alt = enu_to_geodetic(centre, *origin)
            db.insert_contact(conn, lat, lon, alt, nodes, track_id=track_id, node_time_ms=t_ms)
            written += 1
    return written


def step(conn, watermark: int, pending: list[dict]) -> tuple[int, list[dict]]:
    """One pass of the background loop: close stale tracks, read what's new,
    triangulate every bucket that is complete. Returns the new watermark and
    the detections still held back. Split out of run() so tests can drive
    exactly what the Server does, one second at a time."""
    # A drone that leaves every camera's view sends nothing at all, so close
    # tracks on this Server's clock too, not only as detections arrive.
    targets.expire_stale(conn)

    new = db.detections_after(conn, watermark)
    if new:
        watermark = new[-1]["id"]
        pending = pending + new
    if not pending:
        return watermark, pending

    # Hold back the newest bucket so late arrivals can still join it.
    newest_bucket = max(d["node_time_ms"] // BUCKET_MS for d in pending)
    ready = [d for d in pending if d["node_time_ms"] // BUCKET_MS < newest_bucket]
    pending = [d for d in pending if d["node_time_ms"] // BUCKET_MS == newest_bucket]

    if ready:
        count = process(conn, ready)
        if count:
            print(f"tracked {count} contact(s)", flush=True)
    return watermark, pending


async def run(conn) -> None:
    """Background loop. Started by the Server at boot."""
    watermark = db.latest_detection_id(conn)
    pending: list[dict] = []
    print(f"tracker started (from detection #{watermark})", flush=True)

    while True:
        await asyncio.sleep(POLL_S)
        watermark, pending = step(conn, watermark, pending)
