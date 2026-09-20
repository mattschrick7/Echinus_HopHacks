"""Explainable behavior labels for tracked targets.

These labels describe motion patterns, not confirmed object identities. They are
intentionally conservative until a target has enough trajectory history.
"""
from __future__ import annotations

import math


MIN_POINTS = 3  # matches targets.CONFIRM_HITS, so confirmation alerts can be classified


def _bearing_change(previous: tuple[float, float], current: tuple[float, float]) -> float:
    previous_heading = math.atan2(previous[0], previous[1])
    current_heading = math.atan2(current[0], current[1])
    return abs((current_heading - previous_heading + math.pi) % (2 * math.pi) - math.pi)


def classify_target(target: dict, contacts: list[dict]) -> dict:
    """Return a motion label, confidence, and human-readable evidence."""
    if len(contacts) < MIN_POINTS:
        return {
            "classification": "unknown",
            "confidence": 0.2,
            "classification_reasons": ["not enough trajectory history"],
        }

    origin = (contacts[0]["lat"], contacts[0]["lon"], contacts[0].get("alt_m") or 0.0)
    # Import here to keep this module usable without making geometry part of its API.
    from geometry import geodetic_to_enu

    positions = [
        geodetic_to_enu(c["lat"], c["lon"], c.get("alt_m") or 0.0, *origin)
        for c in contacts
    ]
    segments = []
    headings = []
    for previous, current in zip(positions, positions[1:]):
        dt_ms = (current_contact_time := contacts[len(segments) + 1].get("node_time_ms")) - contacts[len(segments)].get("node_time_ms")
        if dt_ms <= 0:
            continue
        vector = current - previous
        distance = float((vector @ vector) ** 0.5)
        segments.append((distance, dt_ms / 1000.0))
        headings.append((float(vector[0]), float(vector[1])))

    if len(segments) < 2:
        return {
            "classification": "unknown",
            "confidence": 0.25,
            "classification_reasons": ["trajectory timestamps are too sparse"],
        }

    speeds = [distance / seconds for distance, seconds in segments]
    path_length = sum(distance for distance, _seconds in segments)
    displacement = float(((positions[-1] - positions[0]) @ (positions[-1] - positions[0])) ** 0.5)
    straightness = displacement / path_length if path_length else 1.0
    mean_speed = sum(speeds) / len(speeds)
    speed_variation = (max(speeds) - min(speeds)) / max(mean_speed, 1.0)
    turns = sum(_bearing_change(previous, current) >= math.radians(25) for previous, current in zip(headings, headings[1:]))
    altitude_range = max(c.get("alt_m") or 0.0 for c in contacts) - min(c.get("alt_m") or 0.0 for c in contacts)
    duration_s = max(0.0, (contacts[-1].get("node_time_ms", 0) - contacts[0].get("node_time_ms", 0)) / 1000.0)

    reasons: list[str]
    label: str
    confidence: float
    if duration_s < 2.0:
        label, confidence = "transient / artifact", 0.68
        reasons = ["short trajectory"]
    elif mean_speed >= 45.0 and straightness >= 0.8 and turns <= 1:
        label, confidence = "aircraft-like", 0.84
        reasons = ["fast", "mostly straight path"]
    elif turns >= 2 or (speed_variation >= 0.65 and mean_speed < 35.0):
        label, confidence = "bird-like", 0.72
        reasons = ["frequent direction or speed changes"]
    elif straightness < 0.2:
        label, confidence = "transient / artifact", 0.68
        reasons = ["unstable trajectory"]
    elif mean_speed < 45.0 and straightness >= 0.7:
        label, confidence = "drone-like", 0.7
        reasons = ["controlled, persistent path"]
    else:
        label, confidence = "unknown", 0.4
        reasons = ["motion does not match a strong pattern"]

    if altitude_range >= 100.0:
        reasons.append("changing altitude")
    if target.get("speed_mps") is not None:
        reasons.append(f"estimated speed {target['speed_mps']:.0f} m/s")

    return {
        "classification": label,
        "confidence": confidence,
        "classification_reasons": reasons,
    }
