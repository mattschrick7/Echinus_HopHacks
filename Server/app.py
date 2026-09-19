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
import tracker
from geometry import DEFAULT_FOV_H_DEG, DEFAULT_FOV_V_DEG, camera_to_world_azel, view_footprint

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
    """Store one message from a hub.

    A detection's camera-relative angles are saved exactly as reported. If the
    node has been positioned, the world bearing is computed and saved too —
    that's the column the tracker reads. If it hasn't, the detection is still
    kept: fill the node's position in later and new detections start counting.
    """
    node_id = message.get("node_id")
    if not node_id:
        return

    node = db.ensure_node(conn, node_id)
    if message["type"] != "detect":
        return  # heartbeats just refresh last_seen, which ensure_node did

    world = None
    if node["configured"]:
        world = camera_to_world_azel(
            message["az_deg"], message["el_deg"],
            node["yaw_deg"], node["pitch_deg"], node["roll_deg"],
        )

    db.insert_detection(
        conn,
        node_id=node_id,
        hub_id=message.get("hub_id"),
        node_time_ms=message.get("timestamp_ms", 0),
        cam_az_deg=message["az_deg"],
        cam_el_deg=message["el_deg"],
        world=world,
    )


@app.websocket("/ws/hub")
async def hub_socket(ws: WebSocket) -> None:
    """One connection per hub. Messages are the packet dicts from echinus_link."""
    await ws.accept()
    hub_id = "unknown"
    try:
        while True:
            message = json.loads(await ws.receive_text())
            if message.get("type") == "hello":
                hub_id = message.get("hub_id", hub_id)
                connected_hubs.add(hub_id)
                print(f"hub connected: {hub_id}", flush=True)
                continue
            record(message)
    except (WebSocketDisconnect, json.JSONDecodeError, KeyError) as exc:
        if not isinstance(exc, WebSocketDisconnect):
            print(f"hub {hub_id} sent something unusable: {exc}", flush=True)
    finally:
        connected_hubs.discard(hub_id)
        print(f"hub disconnected: {hub_id}", flush=True)


# ── operator API ─────────────────────────────────────────────────────────────

def with_footprint(node: dict | None) -> dict | None:
    """Attach the outline of what the node's camera can see, for the map."""
    if node and node["configured"]:
        node["footprint"] = view_footprint(
            node["lat"], node["lon"], node["yaw_deg"], node["pitch_deg"], node["roll_deg"],
            node["fov_h_deg"], node["fov_v_deg"],
        )
    return node


@app.get("/api/nodes")
def api_nodes() -> list[dict]:
    return [with_footprint(n) for n in db.list_nodes(conn)]


@app.get("/api/footprint")
def api_footprint(
    lat: float, lon: float, yaw_deg: float, pitch_deg: float, roll_deg: float = 0.0,
    fov_h_deg: float = DEFAULT_FOV_H_DEG, fov_v_deg: float = DEFAULT_FOV_V_DEG,
) -> list[tuple[float, float]]:
    """The same outline for values not saved yet: the editor's live preview."""
    return view_footprint(lat, lon, yaw_deg, pitch_deg, roll_deg, fov_h_deg, fov_v_deg)


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


@app.get("/api/status")
def api_status() -> dict:
    return {
        "hubs": sorted(connected_hubs),
        "nodes": len(db.list_nodes(conn)),
        "detections": db.latest_detection_id(conn),
    }
