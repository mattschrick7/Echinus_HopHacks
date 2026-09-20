"""
The Echinus server — dashboard, API, and the one place that knows anything.

Runs on the ground station (a Linux box, in Docker). It does three things:

  * accepts websocket connections from hubs and records what the nodes saw
  * lets the operator say where each node is and which way it points
  * serves the dashboard those two things feed

Everything a detection *means* is worked out here, because only here do we
know a node's position and orientation. A node reports "something moved 8
degrees left of where I'm looking"; this server knows where it's looking.

    uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import db
import ingest
import tracker
from geometry import (
    DEFAULT_FOV_H_DEG,
    DEFAULT_FOV_V_DEG,
    DETECTION_RANGE_M,
    view_cone,
    view_footprint,
)

HERE = Path(__file__).parent

conn = db.connect()

# Hubs currently connected, for the dashboard's status line.
connected_hubs: set[str] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(tracker.run(conn))
    print("server ready", flush=True)
    yield
    task.cancel()


app = FastAPI(title="Echinus", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


# ── dashboard ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (HERE / "templates" / "index.html").read_text(encoding="utf-8")


# ── hub ingest ───────────────────────────────────────────────────────────────

def record(message: dict) -> None:
    """Store one message from a hub. The work is in ingest.py."""
    ingest.record(conn, message)


@app.websocket("/ws/hub")
async def hub_socket(ws: WebSocket) -> None:
    """One connection per hub. Messages are the packet dicts from echinus_link."""
    await ws.accept()
    hub_id = "unknown"
    try:
        while True:
            raw = await ws.receive_text()
            try:
                message = json.loads(raw)
                if message.get("type") == "hello":
                    hub_id = message.get("hub_id", hub_id)
                    connected_hubs.add(hub_id)
                    print(f"hub connected: {hub_id}", flush=True)
                    continue
                record(message)
            except Exception as exc:
                # One unusable message must cost one message. This used to
                # escape the loop and close the socket, and the hub's reconnect
                # backoff then threw away five seconds of everyone's packets.
                print(f"hub {hub_id} sent something unusable: {exc}", flush=True)
    except WebSocketDisconnect:
        pass
    finally:
        connected_hubs.discard(hub_id)
        print(f"hub disconnected: {hub_id}", flush=True)


# ── operator API ─────────────────────────────────────────────────────────────

def with_footprint(node: dict | None) -> dict | None:
    """Attach what the node's camera can see: the outline the map draws flat,
    and the same pyramid's far corners for the 3D view to draw in the air."""
    if node and node["configured"]:
        node["footprint"] = view_footprint(
            node["lat"], node["lon"], node["yaw_deg"], node["pitch_deg"], node["roll_deg"],
            node["fov_h_deg"], node["fov_v_deg"], node["range_m"],
        )
        node["view_cone"] = view_cone(
            node["lat"], node["lon"], node["alt_m"],
            node["yaw_deg"], node["pitch_deg"], node["roll_deg"],
            node["fov_h_deg"], node["fov_v_deg"], node["range_m"],
        )
    return node


@app.get("/api/nodes")
def api_nodes() -> list[dict]:
    return [with_footprint(n) for n in db.list_nodes(conn)]


@app.get("/api/footprint")
def api_footprint(
    lat: float, lon: float, yaw_deg: float, pitch_deg: float, roll_deg: float = 0.0,
    fov_h_deg: float = DEFAULT_FOV_H_DEG, fov_v_deg: float = DEFAULT_FOV_V_DEG,
    range_m: float = DETECTION_RANGE_M,
) -> list[tuple[float, float]]:
    """The same outline for values not saved yet: the editor's live preview."""
    return view_footprint(lat, lon, yaw_deg, pitch_deg, roll_deg, fov_h_deg, fov_v_deg, range_m)


@app.get("/api/view-cone")
def api_view_cone(
    lat: float, lon: float, yaw_deg: float, pitch_deg: float,
    alt_m: float = 0.0, roll_deg: float = 0.0,
    fov_h_deg: float = DEFAULT_FOV_H_DEG, fov_v_deg: float = DEFAULT_FOV_V_DEG,
    range_m: float = DETECTION_RANGE_M,
) -> list[tuple[float, float, float]]:
    """And the 3D corners for those same unsaved values, for the 3D preview."""
    return view_cone(lat, lon, alt_m, yaw_deg, pitch_deg, roll_deg,
                     fov_h_deg, fov_v_deg, range_m)


@app.post("/api/nodes")
def api_create_node(body: dict = Body(...)) -> dict:
    """Add a node before its hardware exists. Body needs at least node_id."""
    node_id = (body.get("node_id") or "").strip()
    if not node_id:
        raise HTTPException(400, "node_id is required")
    if len(node_id) > 12:
        raise HTTPException(400, "node_id must be 12 characters or fewer")
    return with_footprint(db.create_node(conn, node_id, body))


@app.patch("/api/nodes/{node_id}")
def api_update_node(node_id: str, body: dict = Body(...)) -> dict:
    """Set a node's position, orientation, name or notes — the operator's edit."""
    if db.get_node(conn, node_id) is None:
        raise HTTPException(404, f"no node {node_id}")
    return with_footprint(db.update_node(conn, node_id, body))


@app.delete("/api/nodes/{node_id}")
def api_delete_node(node_id: str) -> dict:
    db.delete_node(conn, node_id)
    return {"deleted": node_id}


@app.get("/api/detections")
def api_detections(limit: int = 200) -> list[dict]:
    return db.list_detections(conn, limit)


@app.get("/api/contacts")
def api_contacts(limit: int = 200, max_age_s: float | None = None) -> list[dict]:
    return db.list_contacts(conn, limit, max_age_s)


@app.get("/api/targets")
def api_targets(max_age_s: float | None = None) -> list[dict]:
    """Contacts chained into drones (targets.py). Every target ever confirmed,
    lost ones included; pass max_age_s to leave out lost ones older than that."""
    return db.list_targets(conn, max_age_s)


@app.get("/api/targets/{track_id}/contacts")
def api_target_contacts(track_id: int) -> list[dict]:
    """Every contact that makes up one target: its flight path, oldest first."""
    if db.get_track(conn, track_id) is None:
        raise HTTPException(404, f"no target {track_id}")
    return db.track_contacts(conn, track_id)


@app.get("/api/status")
def api_status() -> dict:
    return {
        "hubs": sorted(connected_hubs),
        "nodes": len(db.list_nodes(conn)),
        "detections": db.latest_detection_id(conn),
    }
