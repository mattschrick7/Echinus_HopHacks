"""
The Server's database — SQLite, one file, four tables.

This file is the source of truth for the whole system. Nodes and hubs hold no
persistent state; everything that matters is here.

    nodes        who exists and, crucially, where each node is and which way it
                 points. The operator owns these fields — nothing on the radio
                 can change them. A node that transmits before anyone has
                 configured it is inserted with placeholder values and
                 configured = 0, so it shows up in the dashboard waiting to be
                 positioned.
    detections   every az/el a node has reported. The camera-relative angles
                 are stored as received; the world bearing is stored alongside
                 once the node has a position and orientation.
    contacts     positions worked out by crossing detections from two or more
                 nodes. Written by the tracker.
    tracks       contacts chained over time into one object each — a target.
                 This table is the association state (targets.py), so a
                 restarted Server carries on the same targets.

SQLite because this is one always-on box with one writer. WAL mode lets the
dashboard read while detections stream in.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

DB_PATH = os.environ.get("ECHINUS_DB", "/data/echinus.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    lat         REAL NOT NULL DEFAULT 0,
    lon         REAL NOT NULL DEFAULT 0,
    alt_m       REAL NOT NULL DEFAULT 0,
    yaw_deg     REAL NOT NULL DEFAULT 0,    -- compass bearing the lens points
    pitch_deg   REAL NOT NULL DEFAULT 0,    -- degrees above the horizon
    roll_deg    REAL NOT NULL DEFAULT 0,    -- rotation about the lens axis
    fov_h_deg   REAL NOT NULL DEFAULT 62.2, -- camera field of view, across
    fov_v_deg   REAL NOT NULL DEFAULT 48.8, -- and up-down
    range_m     REAL NOT NULL DEFAULT 1500, -- how far off it can pick out a drone
    configured  INTEGER NOT NULL DEFAULT 0, -- operator has set position + orientation
    enabled     INTEGER NOT NULL DEFAULT 1, -- include this node in tracking
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT,
    -- Health, measured from real traffic; never typed in.
    clock_offset_ms  REAL,  -- node clock minus hub clock, smoothed; NULL = unknown
    out_of_view_at   TEXT,  -- last time it reported an angle outside its set view
    out_of_view_note TEXT   -- and what that angle was
);

CREATE TABLE IF NOT EXISTS detections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id       TEXT NOT NULL,
    hub_id        TEXT,
    node_time_ms  INTEGER NOT NULL,        -- the node's own clock
    received_at   TEXT NOT NULL DEFAULT (datetime('now')),
    cam_az_deg    REAL NOT NULL,           -- as reported: relative to the lens
    cam_el_deg    REAL NOT NULL,
    world_az_deg  REAL,                    -- NULL until the node is configured
    world_el_deg  REAL
);
CREATE INDEX IF NOT EXISTS idx_detections_id_time ON detections (node_time_ms);

CREATE TABLE IF NOT EXISTS contacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    alt_m        REAL,
    node_count   INTEGER NOT NULL,
    node_ids     TEXT NOT NULL DEFAULT ''  -- comma-separated: who saw it
);
CREATE INDEX IF NOT EXISTS idx_contacts_observed_at ON contacts (observed_at);

CREATE TABLE IF NOT EXISTS tracks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    number        INTEGER,                 -- display id T-n; NULL until confirmed
    status        TEXT NOT NULL,           -- tentative | active | lost | dropped
    first_ms      INTEGER NOT NULL,        -- node time of the first contact
    last_ms       INTEGER NOT NULL,        -- and of the latest
    lat           REAL NOT NULL,           -- smoothed position
    lon           REAL NOT NULL,
    alt_m         REAL,
    vel_e         REAL,                    -- smoothed velocity, m/s; NULL until
    vel_n         REAL,                    -- the second contact
    vel_u         REAL,
    contact_count INTEGER NOT NULL DEFAULT 0,
    node_ids      TEXT NOT NULL DEFAULT '', -- every node that has seen it
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks (status);
"""

# Indexes on columns added by LATER_COLUMNS, created once those columns exist.
LATER_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_contacts_track ON contacts (track_id);
"""

# Fields the operator may edit from the dashboard. Everything else about a node
# is either its identity or is derived from traffic.
EDITABLE = ("name", "lat", "lon", "alt_m", "yaw_deg", "pitch_deg", "roll_deg",
            "fov_h_deg", "fov_v_deg", "range_m", "enabled", "notes")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
# existing table alone, so a database from before them needs them added.
LATER_COLUMNS = {
    "nodes": {
        "fov_h_deg": "REAL NOT NULL DEFAULT 62.2",
        "fov_v_deg": "REAL NOT NULL DEFAULT 48.8",
        "range_m": "REAL NOT NULL DEFAULT 1500",
        "clock_offset_ms": "REAL",
        "out_of_view_at": "TEXT",
        "out_of_view_note": "TEXT",
    },
    "contacts": {
        "node_ids": "TEXT NOT NULL DEFAULT ''",
        "track_id": "INTEGER",       # the target this contact belongs to
        "node_time_ms": "INTEGER",   # node clock of the bucket it came from
    },
}


def _add_missing_columns(conn) -> None:
    for table, columns in LATER_COLUMNS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.executescript(LATER_INDEXES)
    conn.commit()


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params)]


# ── nodes ────────────────────────────────────────────────────────────────────

def list_nodes(conn) -> list[dict]:
    return _rows(conn, "SELECT * FROM nodes ORDER BY node_id")


def get_node(conn, node_id: str) -> dict | None:
    rows = _rows(conn, "SELECT * FROM nodes WHERE node_id = ?", (node_id,))
    return rows[0] if rows else None


def ensure_node(conn, node_id: str) -> dict:
    """Return a node, creating an unconfigured placeholder if it's new.

    Called on every packet: a node that appears on the radio should appear in
    the dashboard immediately, flagged as needing a position.
    """
    conn.execute("INSERT OR IGNORE INTO nodes (node_id, name) VALUES (?, ?)", (node_id, node_id))
    conn.execute("UPDATE nodes SET last_seen = datetime('now') WHERE node_id = ?", (node_id,))
    conn.commit()
    return get_node(conn, node_id)


def update_node(conn, node_id: str, changes: dict[str, Any]) -> dict | None:
    """Apply operator edits. Unknown keys are ignored, not an error.

    Setting a position or orientation marks the node configured, which is what
    lets the tracker start using it.
    """
    fields = {k: v for k, v in changes.items() if k in EDITABLE}
    if not fields:
        return get_node(conn, node_id)

    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE nodes SET {assignments} WHERE node_id = ?", (*fields.values(), node_id))
    if {"fov_h_deg", "fov_v_deg"} & fields.keys():
        # The operator has changed the view; judge the next detections afresh.
        conn.execute("UPDATE nodes SET out_of_view_at = NULL, out_of_view_note = NULL WHERE node_id = ?", (node_id,))

    # A node counts as configured once it has a real position. Latitude and
    # longitude both exactly zero is the default the row starts life with, and
    # as a genuine location it's a patch of ocean off West Africa — so treat it
    # as "still not placed" rather than letting an accidental save on an empty
    # form drop a node into the Atlantic.
    conn.execute(
        "UPDATE nodes SET configured = (lat != 0 OR lon != 0) WHERE node_id = ?",
        (node_id,),
    )
    conn.commit()
    return get_node(conn, node_id)


def set_clock_offset(conn, node_id: str, offset_ms: float) -> None:
    conn.execute("UPDATE nodes SET clock_offset_ms = ? WHERE node_id = ?", (offset_ms, node_id))
    conn.commit()


def flag_out_of_view(conn, node_id: str, note: str) -> None:
    conn.execute(
        "UPDATE nodes SET out_of_view_at = datetime('now'), out_of_view_note = ? WHERE node_id = ?",
        (note, node_id),
    )
    conn.commit()


def create_node(conn, node_id: str, changes: dict[str, Any]) -> dict:
    """Add a node by hand, before its hardware is ever switched on."""
    conn.execute("INSERT OR IGNORE INTO nodes (node_id, name) VALUES (?, ?)", (node_id, node_id))
    conn.commit()
    return update_node(conn, node_id, changes)


def delete_node(conn, node_id: str) -> None:
    conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
    conn.commit()


# ── detections ───────────────────────────────────────────────────────────────

def insert_detection(
    conn,
    node_id: str,
    hub_id: str | None,
    node_time_ms: int,
    cam_az_deg: float,
    cam_el_deg: float,
    world: tuple[float, float] | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO detections "
        "(node_id, hub_id, node_time_ms, cam_az_deg, cam_el_deg, world_az_deg, world_el_deg) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (node_id, hub_id, node_time_ms, cam_az_deg, cam_el_deg,
         world[0] if world else None, world[1] if world else None),
    )
    conn.commit()
    return cur.lastrowid


def list_detections(conn, limit: int = 200) -> list[dict]:
    return _rows(conn, "SELECT * FROM detections ORDER BY id DESC LIMIT ?", (limit,))


def detections_after(conn, after_id: int, limit: int = 5000) -> list[dict]:
    """Detections newer than `after_id`, oldest first — how the tracker keeps
    its place in the stream."""
    return _rows(
        conn,
        "SELECT * FROM detections WHERE id > ? AND world_az_deg IS NOT NULL "
        "ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    )


def latest_detection_id(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM detections").fetchone()
    return row["id"]


# ── contacts ─────────────────────────────────────────────────────────────────

def insert_contact(
    conn,
    lat: float,
    lon: float,
    alt_m: float | None,
    node_ids,
    track_id: int | None = None,
    node_time_ms: int | None = None,
) -> int:
    node_ids = sorted(node_ids)
    cur = conn.execute(
        "INSERT INTO contacts (lat, lon, alt_m, node_count, node_ids, track_id, node_time_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lat, lon, alt_m, len(node_ids), ",".join(node_ids), track_id, node_time_ms),
    )
    conn.commit()
    return cur.lastrowid


def list_contacts(conn, limit: int = 200, max_age_s: float | None = None) -> list[dict]:
    """Newest first. `age_s` is worked out here, on the Server's clock, so the
    dashboard can fade contacts without trusting the browser's clock.
    `max_age_s` drops anything older."""
    rows = _rows(
        conn,
        "SELECT *, (julianday('now') - julianday(observed_at)) * 86400.0 AS age_s "
        "FROM contacts WHERE ? IS NULL OR observed_at >= datetime('now', ?) "
        "ORDER BY id DESC LIMIT ?",
        (max_age_s, f"-{max_age_s or 0} seconds", limit),
    )
    for row in rows:
        row["node_ids"] = [n for n in row["node_ids"].split(",") if n]
    return rows


def track_contacts(conn, track_id: int) -> list[dict]:
    """One target's whole flight path, oldest first."""
    rows = _rows(
        conn,
        "SELECT *, (julianday('now') - julianday(observed_at)) * 86400.0 AS age_s "
        "FROM contacts WHERE track_id = ? ORDER BY COALESCE(node_time_ms, 0), id",
        (track_id,),
    )
    for row in rows:
        row["node_ids"] = [n for n in row["node_ids"].split(",") if n]
    return rows


# ── tracks ───────────────────────────────────────────────────────────────────

def _track(row: dict) -> dict:
    row["node_ids"] = [n for n in row["node_ids"].split(",") if n]
    return row


def get_track(conn, track_id: int) -> dict | None:
    rows = _rows(conn, "SELECT * FROM tracks WHERE id = ?", (track_id,))
    return _track(rows[0]) if rows else None


def open_tracks(conn) -> list[dict]:
    """Tracks still able to take contacts: tentative and active."""
    return [_track(r) for r in _rows(
        conn, "SELECT * FROM tracks WHERE status IN ('tentative', 'active') ORDER BY id")]


def insert_track(conn, t_ms: int, lat: float, lon: float, alt_m: float | None, node_ids) -> int:
    cur = conn.execute(
        "INSERT INTO tracks (status, first_ms, last_ms, lat, lon, alt_m, contact_count, node_ids) "
        "VALUES ('tentative', ?, ?, ?, ?, ?, 1, ?)",
        (t_ms, t_ms, lat, lon, alt_m, ",".join(sorted(node_ids))),
    )
    conn.commit()
    return cur.lastrowid


TRACK_FIELDS = ("number", "status", "last_ms", "lat", "lon", "alt_m",
                "vel_e", "vel_n", "vel_u", "contact_count", "node_ids")


def update_track(conn, track_id: int, changes: dict[str, Any]) -> None:
    fields = {k: v for k, v in changes.items() if k in TRACK_FIELDS}
    if "node_ids" in fields and not isinstance(fields["node_ids"], str):
        fields["node_ids"] = ",".join(sorted(fields["node_ids"]))
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE tracks SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        (*fields.values(), track_id),
    )
    conn.commit()


def close_tracks(conn, older_than_ms: int) -> None:
    """Tracks with no contact since `older_than_ms` are over: numbered ones are
    lost, unconfirmed ones dropped. updated_at is left alone, so a lost target
    ages from its last contact."""
    conn.execute(
        "UPDATE tracks SET status = CASE WHEN number IS NULL THEN 'dropped' ELSE 'lost' END "
        "WHERE status IN ('tentative', 'active') AND last_ms < ?",
        (older_than_ms,),
    )
    conn.commit()


def next_target_number(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM tracks").fetchone()
    return row["n"]


def list_targets(conn, max_age_s: float | None = None) -> list[dict]:
    """Confirmed tracks — the ones worth showing as targets. Targets are
    permanent: lost ones stay listed, with their whole path, unless the caller
    passes `max_age_s` to leave out lost ones older than that. Ones still being
    tracked come first, then the rest newest first."""
    rows = _rows(
        conn,
        "SELECT *, (julianday('now') - julianday(updated_at)) * 86400.0 AS age_s "
        "FROM tracks WHERE number IS NOT NULL AND status != 'dropped' "
        "AND (status = 'active' OR ? IS NULL OR updated_at >= datetime('now', ?)) "
        "ORDER BY status = 'active' DESC, number DESC",
        (max_age_s, f"-{max_age_s or 0} seconds"),
    )
    for row in rows:
        _track(row)
        v = [row["vel_e"], row["vel_n"], row["vel_u"]]
        row["speed_mps"] = None if None in v else sum(x * x for x in v) ** 0.5
    return rows
