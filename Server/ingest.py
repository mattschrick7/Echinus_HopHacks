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
    if message.get("type") != "detect":
        return  # heartbeats just refresh last_seen, which ensure_node did

    # Guarded rather than subscripted. A malformed message used to raise out of
    # here and tear down the hub's whole websocket — see app.hub_socket — so
    # one bad field cost every packet from that hub for the five seconds its
    # reconnect took.
    az, el = message.get("az_deg"), message.get("el_deg")
    if not isinstance(az, (int, float)) or not isinstance(el, (int, float)):
        return
    node_time_ms = message.get("timestamp_ms", 0)
    if not isinstance(node_time_ms, int):
        return

    # Nodes no longer send a clock reading, so this is no longer clock skew:
    # it is how long a packet took to get here, and it is always negative. A
    # large magnitude means the channel is congested — the node's transmission
    # sat behind listen-before-talk and the queue — which is a more useful
    # thing to watch than the drift this used to measure.
    if "received_ms" in message:  # set by a real hub, not by the simulator
        offset = node_time_ms - message["received_ms"]
        previous = node["clock_offset_ms"]
        smoothed = offset if previous is None else previous + CLOCK_SMOOTHING * (offset - previous)
        db.set_clock_offset(conn, node_id, smoothed)

    if abs(az) > node["fov_h_deg"] / 2 + VIEW_SLACK_DEG or abs(el) > node["fov_v_deg"] / 2 + VIEW_SLACK_DEG:
        db.flag_out_of_view(conn, node_id, f"az {az:.1f}° el {el:.1f}°")

    # Two hubs in earshot of one node both forward its packet. It is one
    # detection; stored twice, the copies would cross each other's partners
    # and make a second contact — and a second target — at the same spot.
    #
    # Real packets carry a sequence number and a target id, which identify an
    # observation exactly. The simulator speaks straight to the websocket and
    # has neither, so it falls back to matching on time and angle.
    seq, target_id = message.get("seq"), message.get("target_id")
    if seq is not None and target_id is not None:
        if db.packet_seen(conn, node_id, seq, target_id, node_time_ms):
            return
    elif db.detection_exists(conn, node_id, node_time_ms, az, el):
        return

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
        seq=seq,
        target_id=target_id,
    )
