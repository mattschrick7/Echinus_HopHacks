"""
The Server's database — SQLite, one file, three tables.

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
    configured  INTEGER NOT NULL DEFAULT 0, -- operator has set position + orientation
    enabled     INTEGER NOT NULL DEFAULT 1, -- include this node in tracking
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT
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
"""

# Fields the operator may edit from the dashboard. Everything else about a node
# is either its identity or is derived from traffic.
EDITABLE = ("name", "lat", "lon", "alt_m", "yaw_deg", "pitch_deg", "roll_deg",
            "fov_h_deg", "fov_v_deg", "enabled", "notes")


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
    },
    "contacts": {
        "node_ids": "TEXT NOT NULL DEFAULT ''",
    },
}


def _add_missing_columns(conn) -> None:
    for table, columns in LATER_COLUMNS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
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

def insert_contact(conn, lat: float, lon: float, alt_m: float | None, node_ids) -> None:
    node_ids = sorted(node_ids)
    conn.execute(
        "INSERT INTO contacts (lat, lon, alt_m, node_count, node_ids) VALUES (?, ?, ?, ?, ?)",
        (lat, lon, alt_m, len(node_ids), ",".join(node_ids)),
    )
    conn.commit()


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
