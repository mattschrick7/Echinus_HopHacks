"""
Turning contacts into targets: which dots are the same drone.

The tracker (tracker.py) works one instant at a time: each 200 ms bucket of
bearings becomes zero or more contacts, and nothing links one to the next. This
file chains them. Each chain is a *track*; once a track has proved itself it is
a *target*, numbered T-1, T-2, … for the dashboard.

Per bucket, with the contacts it produced:
  1. Tracks that haven't been seen for LOST_AFTER_MS are closed: numbered ones
     become "lost" (kept for good, so the operator can always look back at
     where they went), tentative ones are "dropped" and never shown.
  2. Every open track predicts where it should be now, from its smoothed
     position and velocity.
  3. A contact may join a track only inside a gate around that prediction. The
     gate is wide for a brand-new track (we don't know its velocity yet, only
     that it can't be faster than a drone) and narrower once it has one.
  4. Contacts and tracks are paired cheapest first, each used once — two drones
     in the air together must never share a contact, the same rule tracker.py
     applies to rays. Being seen by the same cameras as last time makes a pair
     slightly cheaper, which breaks near-ties the right way.
  5. A matched track moves toward the contact (an alpha-beta filter: nudge the
     position, nudge the velocity). A contact that matched nothing starts a new
     tentative track. CONFIRM_HITS contacts in a row make it a numbered target,
     so a one-off false crossing never takes an ID.

The tracks table is the whole state, so this module holds nothing in memory
and a Server restart picks up the same targets.
"""
from __future__ import annotations

import numpy as np

import db
from geometry import enu_to_geodetic, geodetic_to_enu

# Tuning knobs.
LOST_AFTER_MS = 5_000   # no contact for this long and a track is over
GATE_M = 40.0           # how far a contact may sit from the prediction, plus…
MAX_SPEED_MPS = 60.0    # …this per second while the track's velocity is unknown
MANEUVER_MPS = 15.0     # …or this per second once it's known (turns, speed changes)
NODE_BONUS = 0.2        # up to 20% off the distance for being seen by the same nodes
ALPHA = 0.5             # how far the position moves toward each new contact
BETA = 0.3              # how far the velocity does
CONFIRM_HITS = 3        # contacts before a track is shown as a target
MIN_DT_S = 0.05         # guard against two contacts with the same timestamp

# A fix: one triangulated object in a bucket — ENU position and the nodes that saw it.
Fix = tuple[np.ndarray, set]


def expire(conn, now_ms: int) -> None:
    """Close tracks not seen for LOST_AFTER_MS, as of node time `now_ms`.

    Used as contacts arrive, where everything is on the nodes' clocks."""
    db.close_tracks(conn, now_ms - LOST_AFTER_MS)


def expire_stale(conn) -> None:
    """Close tracks that have had no contact for LOST_AFTER_MS of real time.

    For when nothing arrives at all — a drone out of every camera's view sends
    nothing. Judged on this Server's clock, never the nodes': real nodes' clocks
    can sit seconds off the Server's (NTP drift, LoRa and relay delay), and
    comparing the two would close every track as it opened."""
    db.close_stale_tracks(conn, LOST_AFTER_MS / 1000.0)


def _state(track: dict, origin) -> tuple[np.ndarray, np.ndarray | None]:
    position = geodetic_to_enu(track["lat"], track["lon"], track["alt_m"] or 0.0, *origin)
    if track["vel_e"] is None:
        return position, None
    return position, np.array([track["vel_e"], track["vel_n"], track["vel_u"]])


def _shared(a, b) -> float:
    """How much two sets of node ids overlap, 0..1."""
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def assign(conn, fixes: list[Fix], t_ms: int, origin) -> list[int]:
    """Give every fix in one bucket a track id, creating tracks as needed.

    `origin` is the geodetic centre the fixes' ENU coordinates are relative to.
    Returns one track id per fix, in order.
    """
    expire(conn, t_ms)
    tracks = db.open_tracks(conn)

    predictions = []
    for track in tracks:
        position, velocity = _state(track, origin)
        # Negative when a bucket arrives late, after a newer one: over LoRa
        # that happens. Predict backwards to where it was, then.
        dt = (t_ms - track["last_ms"]) / 1000.0
        predicted = position if velocity is None else position + velocity * dt
        gate = GATE_M + (MAX_SPEED_MPS if velocity is None else MANEUVER_MPS) * abs(dt)
        predictions.append((position, velocity, predicted, gate, dt))

    # Every fix-track pair inside its gate, cheapest first.
    pairs = []
    for f, (point, nodes) in enumerate(fixes):
        for k, track in enumerate(tracks):
            _pos, _vel, predicted, gate, _dt = predictions[k]
            distance = float(np.linalg.norm(point - predicted))
            if distance > gate:
                continue
            cost = distance * (1.0 - NODE_BONUS * _shared(nodes, track["node_ids"]))
            pairs.append((cost, f, k))
    pairs.sort()

    result: list[int | None] = [None] * len(fixes)
    taken: set[int] = set()
    for _cost, f, k in pairs:
        if result[f] is not None or k in taken:
            continue
        taken.add(k)
        result[f] = _update(conn, tracks[k], predictions[k], fixes[f], t_ms, origin)

    for f, (point, nodes) in enumerate(fixes):
        if result[f] is None:
            lat, lon, alt = enu_to_geodetic(point, *origin)
            result[f] = db.insert_track(conn, t_ms, lat, lon, alt, nodes)
    return result


def _update(conn, track: dict, prediction, fix: Fix, t_ms: int, origin) -> int:
    """Move a track toward a new contact, and confirm it if it has earned it."""
    position, velocity, predicted, _gate, dt = prediction
    point, nodes = fix

    if dt <= 0:
        # A late contact: it belongs to the track and joins its path, but it is
        # history — steering the filter with it would yank the velocity.
        pass
    elif velocity is None:
        # Second contact: the first real estimate of how it's moving.
        dt = max(dt, MIN_DT_S)
        velocity = (point - position) / dt
        position = position + ALPHA * (point - position)
    else:
        dt = max(dt, MIN_DT_S)
        residual = point - predicted
        position = predicted + ALPHA * residual
        velocity = velocity + BETA * residual / dt

    changes = {
        "last_ms": max(t_ms, track["last_ms"]),
        "contact_count": track["contact_count"] + 1,
        "node_ids": set(track["node_ids"]) | set(nodes),
    }
    if dt > 0:
        lat, lon, alt = enu_to_geodetic(position, *origin)
        changes.update({"lat": lat, "lon": lon, "alt_m": alt})
        if velocity is not None:
            changes.update({"vel_e": float(velocity[0]), "vel_n": float(velocity[1]),
                            "vel_u": float(velocity[2])})
    if track["status"] == "tentative" and changes["contact_count"] >= CONFIRM_HITS:
        changes["status"] = "active"
        changes["number"] = db.next_target_number(conn)
    db.update_track(conn, track["id"], changes)
    if changes.get("status") == "active" and track["status"] != "active":
        db.queue_target_alert(conn, track["id"])
    return track["id"]
