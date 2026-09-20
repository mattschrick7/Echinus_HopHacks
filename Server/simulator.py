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

from geometry import (
    DEFAULT_FOV_H_DEG,
    DEFAULT_FOV_V_DEG,
    DETECTION_RANGE_M,
    enu_to_geodetic,
    world_to_camera_azel,
)

SERVER_URL = os.environ.get("SERVER_URL", "ws://localhost:8000/ws/hub")
API_URL = SERVER_URL.replace("ws://", "http://").replace("wss://", "https://").rsplit("/ws/", 1)[0]

NODE_COUNT = int(os.environ.get("SIM_NODES", "3"))
# Johns Hopkins Homewood campus, Baltimore — the ring's centre sits on the
# upper quad, so the nodes land around the edge of campus.
BASE_LAT = float(os.environ.get("SIM_LAT", "39.3299"))
BASE_LON = float(os.environ.get("SIM_LON", "-76.6205"))
SEND_HZ = float(os.environ.get("SIM_HZ", "5"))
NOISE_DEG = float(os.environ.get("SIM_NOISE_DEG", "0.1"))
FALSE_POSITIVE_RATE = float(os.environ.get("SIM_FP_RATE", "0.02"))
LOOP_S = float(os.environ.get("SIM_LOOP_S", "100"))
PITCH_DEG = float(os.environ.get("SIM_PITCH", "30"))  # camera tilt above the horizon
RECONNECT_S = 2.0     # wait between attempts when the Server is unreachable

RING_M = 500.0        # nodes sit on a circle this big, for decent geometry
# What each camera can see, the same numbers the Server uses to draw its cone.
# Nodes report pinhole angles, so each axis is checked on its own half-angle.
FOV_H_DEG, FOV_V_DEG = DEFAULT_FOV_H_DEG, DEFAULT_FOV_V_DEG

# Drones, in metres around the ring's centre: starting point and velocity. A
# few hundred metres up at ~30 m/s, crossing the ring in a LOOP_S loop.
TARGETS = [
    {"id": "alpha", "start": np.array([-1500.0, 100.0, 200.0]), "velocity": np.array([30.0, 0.0, 0.0])},
    {"id": "bravo", "start": np.array([150.0, -1500.0, 300.0]), "velocity": np.array([-3.0, 28.0, -1.0])},
]
# Target ids are ground truth for the tests only (tests/test_targets.py). They
# are never sent: a real node can't know which drone it's looking at, so the
# Server has to work that out the same way for simulated ones.


def target_positions(elapsed: float) -> dict[str, np.ndarray]:
    """Where each target really is, in metres around the ring's centre."""
    return {t["id"]: t["start"] + t["velocity"] * elapsed for t in TARGETS}


def build_nodes() -> list[dict]:
    """Nodes on a ring, each tilted up and aimed at the next node round it.

    The cameras face each other — with two nodes, head-on; with more, in a
    chain — so neighbouring views overlap over the ring, which is what the
    tracker needs to cross their bearings.
    """
    nodes = []
    for i in range(NODE_COUNT):
        angle = 2 * math.pi * i / NODE_COUNT
        offset = RING_M * np.array([math.cos(angle), math.sin(angle), 0.0])
        lat, lon, alt = enu_to_geodetic(offset, BASE_LAT, BASE_LON, 10.0)
        nodes.append({
            "node_id": f"sim-{i}",
            "name": f"simulated node {i}",
            "enu": offset,
            "lat": lat, "lon": lon, "alt_m": alt,
            "pitch_deg": PITCH_DEG, "roll_deg": 0.0,
            "fov_h_deg": FOV_H_DEG, "fov_v_deg": FOV_V_DEG, "range_m": DETECTION_RANGE_M,
        })

    for i, node in enumerate(nodes):
        east, north, _ = nodes[(i + 1) % len(nodes)]["enu"] - node["enu"]
        node["yaw_deg"] = round(math.degrees(math.atan2(east, north)) % 360.0, 1)
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
        body = {k: node[k] for k in ("node_id", "lat", "lon", "alt_m",
                                   "yaw_deg", "pitch_deg", "roll_deg", "fov_h_deg", "fov_v_deg", "range_m")}
        body["name"] = node["name"]
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
    for position in target_positions(elapsed).values():
        for node in nodes:
            direction = position - node["enu"]
            if np.linalg.norm(direction) > node["range_m"]:
                continue  # too far off to pick out, as the dashboard's cone shows
            angles = world_to_camera_azel(
                direction / np.linalg.norm(direction),
                node["yaw_deg"], node["pitch_deg"], node["roll_deg"],
            )
            if angles is None:
                continue
            az, el = angles
            if abs(az) > node["fov_h_deg"] / 2 or abs(el) > node["fov_v_deg"] / 2:
                continue  # outside the camera's view
            seen.append((node["node_id"], az + random.gauss(0, NOISE_DEG), el + random.gauss(0, NOISE_DEG)))

    for node in nodes:  # things that aren't there
        if random.random() < FALSE_POSITIVE_RATE:
            seen.append((
                node["node_id"],
                random.uniform(-node["fov_h_deg"] / 2, node["fov_h_deg"] / 2),
                random.uniform(-node["fov_v_deg"] / 2, node["fov_v_deg"] / 2),
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
