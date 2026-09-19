"""
What happens to each message a hub forwards — the real data path.

Split out of app.py so the path real nodes take can be tested without a
running server. The simulator goes through here too, so anything true of
simulated detections is true of real ones.

Besides storing the detection, two checks run here that only real hardware
can fail. The simulator can't trip them, which is exactly why they're needed:

  clock      Detections from different nodes are paired by the nodes' own
             timestamps, so the nodes' clocks must agree. The hub stamps each
             packet as it arrives (received_ms); a node whose timestamps sit
             far from that has lost NTP, and its detections will never pair.
  view       A node works out az/el from the field of view in its node.toml.
             The dashboard draws the cone, and the tracker trusts the aim,
             from the field of view typed in for it on the Server. An angle
             outside that view means the two numbers disagree.
"""
from __future__ import annotations

import db
from geometry import camera_to_world_azel

VIEW_SLACK_DEG = 1.0   # detector rounding; beyond this an angle is out of view
CLOCK_SMOOTHING = 0.1  # weight of each new packet in the running clock offset


def record(conn, message: dict) -> None:
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

    az, el = message["az_deg"], message["el_deg"]
    node_time_ms = message.get("timestamp_ms", 0)

    if "received_ms" in message:  # set by a real hub, not by the simulator
        offset = node_time_ms - message["received_ms"]
        previous = node["clock_offset_ms"]
        smoothed = offset if previous is None else previous + CLOCK_SMOOTHING * (offset - previous)
        db.set_clock_offset(conn, node_id, smoothed)

    if abs(az) > node["fov_h_deg"] / 2 + VIEW_SLACK_DEG or abs(el) > node["fov_v_deg"] / 2 + VIEW_SLACK_DEG:
        db.flag_out_of_view(conn, node_id, f"az {az:.1f}° el {el:.1f}°")

    world = None
    if node["configured"]:
        world = camera_to_world_azel(az, el, node["yaw_deg"], node["pitch_deg"], node["roll_deg"])

    db.insert_detection(
        conn,
        node_id=node_id,
        hub_id=message.get("hub_id"),
        node_time_ms=node_time_ms,
        cam_az_deg=az,
        cam_el_deg=el,
        world=world,
    )
