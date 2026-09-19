"""
Fake hardware, for developing the Server without a field deployment.

It pretends to be a hub carrying a few nodes, flies imaginary targets past
them, and reports what each node's camera would have seen — camera-relative
angles, exactly like a real node, with noise and the occasional false
positive so the tracker's rejection logic gets exercised.

It also configures its own nodes through the operator API on start-up, which
a real deployment would never do — but it means `docker compose --profile demo
up` gives you a working map straight away.

    SERVER_URL=ws://localhost:8000/ws/hub python simulator.py
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
import urllib.request

import numpy as np
import websockets

from geometry import enu_to_geodetic, world_to_camera_azel

SERVER_URL = os.environ.get("SERVER_URL", "ws://localhost:8000/ws/hub")
API_URL = SERVER_URL.replace("ws://", "http://").replace("wss://", "https://").rsplit("/ws/", 1)[0]

NODE_COUNT = int(os.environ.get("SIM_NODES", "3"))
BASE_LAT = float(os.environ.get("SIM_LAT", "37.7749"))
BASE_LON = float(os.environ.get("SIM_LON", "-122.4194"))
SEND_HZ = float(os.environ.get("SIM_HZ", "5"))
NOISE_DEG = float(os.environ.get("SIM_NOISE_DEG", "0.1"))
FALSE_POSITIVE_RATE = float(os.environ.get("SIM_FP_RATE", "0.02"))
LOOP_S = float(os.environ.get("SIM_LOOP_S", "40"))
RECONNECT_S = 2.0     # wait between attempts when the Server is unreachable

RING_M = 500.0        # nodes sit on a circle this big, for decent geometry
HALF_FOV_DEG = 31.0   # what the node's camera can see either side of the axis

# Targets in metres around the ring's centre: starting point and velocity.
TARGETS = [
    {"start": np.array([-4000.0, 0.0, 2500.0]), "velocity": np.array([250.0, 30.0, 0.0])},
    {"start": np.array([0.0, -4000.0, 3000.0]), "velocity": np.array([20.0, 260.0, -5.0])},
]


def build_nodes() -> list[dict]:
    """Nodes on a ring, all staring straight up."""
    nodes = []
    for i in range(NODE_COUNT):
        angle = 2 * math.pi * i / NODE_COUNT
        offset = RING_M * np.array([math.cos(angle), math.sin(angle), 0.0])
        lat, lon, alt = enu_to_geodetic(offset, BASE_LAT, BASE_LON, 10.0)
        nodes.append({
            "node_id": f"sim-{i}",
            "enu": offset,
            "lat": lat, "lon": lon, "alt_m": alt,
            "yaw_deg": 0.0, "pitch_deg": 90.0, "roll_deg": 0.0,
        })
    return nodes


def configure_on_server(nodes: list[dict], attempts: int = 30) -> None:
    """Do what an operator would do in the dashboard: place each node."""
    for attempt in range(attempts):  # the server may still be starting up
        try:
            urllib.request.urlopen(f"{API_URL}/api/status", timeout=2).read()
            break
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(1)

    for node in nodes:
        body = {k: node[k] for k in ("node_id", "lat", "lon", "alt_m", "yaw_deg", "pitch_deg", "roll_deg")}
        body["name"] = f"simulated {node['node_id']}"
        request = urllib.request.Request(
            f"{API_URL}/api/nodes",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    print(f"configured {len(nodes)} simulated node(s) via {API_URL}", flush=True)


def observations(nodes: list[dict], elapsed: float) -> list[tuple[str, float, float]]:
    """What every node sees right now, as (node_id, cam_az, cam_el)."""
    seen = []
    for target in TARGETS:
        position = target["start"] + target["velocity"] * elapsed
        for node in nodes:
            direction = position - node["enu"]
            angles = world_to_camera_azel(
                direction / np.linalg.norm(direction),
                node["yaw_deg"], node["pitch_deg"], node["roll_deg"],
            )
            if angles is None:
                continue
            az, el = angles
            if abs(az) > HALF_FOV_DEG or abs(el) > HALF_FOV_DEG:
                continue  # outside the camera's view
            seen.append((node["node_id"], az + random.gauss(0, NOISE_DEG), el + random.gauss(0, NOISE_DEG)))

    for node in nodes:  # things that aren't there
        if random.random() < FALSE_POSITIVE_RATE:
            seen.append((
                node["node_id"],
                random.uniform(-HALF_FOV_DEG, HALF_FOV_DEG),
                random.uniform(-HALF_FOV_DEG, HALF_FOV_DEG),
            ))
    return seen


async def main() -> None:
    nodes = build_nodes()
    start = time.monotonic()  # outside the loop, so targets carry on across reconnects

    # Survive the Server going away, as a real hub does: `uvicorn --reload`
    # restarts it on every Python edit, and a restart closes this socket.
    while True:
        try:
            # Re-placing the nodes is harmless if they exist, and puts them back
            # if the database was wiped while the Server was down.
            await asyncio.to_thread(configure_on_server, nodes)
            await simulate(nodes, start)
        except Exception as exc:  # any disconnect: log, wait, try again
            print(f"server unreachable ({exc!r}); retrying in {RECONNECT_S:.0f}s", flush=True)
            await asyncio.sleep(RECONNECT_S)


async def simulate(nodes: list[dict], start: float) -> None:
    """Stream detections over one websocket connection until it drops."""
    async with websockets.connect(SERVER_URL) as ws:
        await ws.send(json.dumps({"type": "hello", "hub_id": "sim-hub"}))
        print(f"simulating {len(nodes)} nodes, {len(TARGETS)} targets -> {SERVER_URL}", flush=True)

        while True:
            elapsed = (time.monotonic() - start) % LOOP_S
            timestamp_ms = int(time.time() * 1000)  # one shared instant per tick

            for node_id, az, el in observations(nodes, elapsed):
                await ws.send(json.dumps({
                    "type": "detect",
                    "node_id": node_id,
                    "timestamp_ms": timestamp_ms,
                    "az_deg": az,
                    "el_deg": el,
                    "hub_id": "sim-hub",
                }))

            await asyncio.sleep(1.0 / SEND_HZ)


if __name__ == "__main__":
    asyncio.run(main())
