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
     pairs that actually come close to meeting, in front of both cameras, at a
     believable height. Pairs that fail are thrown away — a lone node seeing
     something is not evidence of an object's position.
  4. Merge nearby crossings so an object seen by three nodes is one contact
     with node_count = 3, not three separate contacts.

The newest bucket is held back one cycle so a straggling detection has a chance
to join it before the bucket is closed.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import numpy as np

import db
from geometry import azel_to_unit, closest_approach, enu_to_geodetic, geodetic_to_enu, mean_position

# Tuning knobs. These are the numbers to reach for when tracking looks wrong.
POLL_S = 1.0                 # how often to look for new detections
BUCKET_MS = 200              # detections this close in time are "simultaneous"
MAX_GAP_M = 150.0            # two rays count as meeting if they pass this close
MERGE_M = 300.0              # crossings this close describe the same object
MIN_NODES = 2                # nodes that must agree before a contact is written
MIN_ALT_M, MAX_ALT_M = 0.0, 30_000.0
MAX_RANGE_M = 50_000.0


def _believable(point: np.ndarray) -> bool:
    return MIN_ALT_M <= point[2] <= MAX_ALT_M and np.hypot(point[0], point[1]) <= MAX_RANGE_M


def crossings(rays: list[tuple[str, np.ndarray, np.ndarray]]) -> list[tuple[np.ndarray, frozenset]]:
    """Every cross-node ray pair that genuinely meets somewhere plausible."""
    found = []
    for i, (node_a, origin_a, direction_a) in enumerate(rays):
        for node_b, origin_b, direction_b in rays[i + 1:]:
            if node_a == node_b:
                continue  # a node can't triangulate against itself
            result = closest_approach(origin_a, direction_a, origin_b, direction_b)
            if result is None:
                continue
            midpoint, gap, t_a, t_b = result
            if gap > MAX_GAP_M or t_a < 0 or t_b < 0 or not _believable(midpoint):
                continue
            found.append((midpoint, frozenset((node_a, node_b))))
    return found


def merge(found: list[tuple[np.ndarray, frozenset]]) -> list[tuple[np.ndarray, set]]:
    """Group crossings that are within MERGE_M of each other into one object."""
    groups: list[dict] = []
    for point, nodes in found:
        for group in groups:
            if np.linalg.norm(point - group["centre"]) <= MERGE_M:
                group["points"].append(point)
                group["nodes"] |= nodes
                group["centre"] = np.mean(group["points"], axis=0)
                break
        else:
            groups.append({"points": [point], "nodes": set(nodes), "centre": point})
    return [(g["centre"], g["nodes"]) for g in groups]


def _node_positions(conn) -> tuple[dict[str, np.ndarray], tuple[float, float, float]] | None:
    """Configured, enabled nodes as ENU positions around their shared centre."""
    nodes = [n for n in db.list_nodes(conn) if n["configured"] and n["enabled"]]
    if len(nodes) < MIN_NODES:
        return None
    origin = mean_position((n["lat"], n["lon"], n["alt_m"]) for n in nodes)
    return {
        n["node_id"]: geodetic_to_enu(n["lat"], n["lon"], n["alt_m"], *origin)
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
    for group in buckets.values():
        rays = [
            (d["node_id"], node_enu[d["node_id"]], azel_to_unit(d["world_az_deg"], d["world_el_deg"]))
            for d in group
            if d["node_id"] in node_enu
        ]
        for centre, nodes in merge(crossings(rays)):
            if len(nodes) < MIN_NODES:
                continue
            lat, lon, alt = enu_to_geodetic(centre, *origin)
            db.insert_contact(conn, lat, lon, alt, len(nodes))
            written += 1
    return written


async def run(conn) -> None:
    """Background loop. Started by the Server at boot."""
    watermark = db.latest_detection_id(conn)
    pending: list[dict] = []
    print(f"tracker started (from detection #{watermark})", flush=True)

    while True:
        await asyncio.sleep(POLL_S)

        new = db.detections_after(conn, watermark)
        if new:
            watermark = new[-1]["id"]
            pending.extend(new)
        if not pending:
            continue

        # Hold back the newest bucket so late arrivals can still join it.
        newest_bucket = max(d["node_time_ms"] // BUCKET_MS for d in pending)
        ready = [d for d in pending if d["node_time_ms"] // BUCKET_MS < newest_bucket]
        pending = [d for d in pending if d["node_time_ms"] // BUCKET_MS == newest_bucket]

        if ready:
            count = process(conn, ready)
            if count:
                print(f"tracked {count} contact(s)", flush=True)
