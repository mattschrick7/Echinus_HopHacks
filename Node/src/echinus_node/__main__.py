"""
Echinus node — watch the sky, send what moves over LoRa.

The whole node is this loop:

    capture frame -> MotionDetector -> (az, el) -> LoRa packet -> Hub

A node knows exactly one thing about itself: its id. It has no GPS, no
compass, no calibration step and no network config. Where it sits and which
way it points are recorded by the operator on the Server, which is the single
source of truth for all of that.

Run:
    echinus-node --config node.toml
    echinus-node --config node.toml --dry-run --preview    # no radio, browser view
    echinus-node --config node.toml --beacon               # radio only, no camera
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
import tomllib

from echinus_link import packets

from echinus_node.camera import EndOfStream, open_camera
from echinus_node.detector import MotionDetector

HEARTBEAT_S = 60.0  # how often to tell the Server we're alive when nothing moves
BEACON_S = 2.0      # how often --beacon transmits


class _DryRunRadio:
    """Stand-in for the LoRa HAT so the node runs on a laptop."""

    def send(self, data: bytes) -> None:
        print(f"  would transmit {len(data)}B: {data.hex(' ')}")

    def close(self) -> None:
        pass


def _load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _open_radio(cfg: dict, dry_run: bool):
    if dry_run:
        print("dry run — packets are printed, not transmitted")
        return _DryRunRadio()
    from echinus_link.radio import from_config

    return from_config(cfg.get("lora", {}))


def beacon(cfg: dict, radio, count: int | None) -> None:
    """Transmit heartbeats and nothing else, with no camera involved.

    This is the other half of `echinus-hub --listen`: it proves the two radios
    reach each other, in the real packet format, before a camera or a Server is
    in the picture. Waveshare's own demo can read these too — its receiver
    prints the sender address and channel from the three header bytes we send
    ahead of every packet.
    """
    node_id = cfg["node"]["id"]
    started = time.monotonic()
    sent = 0

    print(f"[{node_id}] beacon every {BEACON_S:.0f}s — Ctrl-C to stop", flush=True)
    try:
        while count is None or sent < count:
            uptime = int(time.monotonic() - started)
            radio.send(packets.encode_heartbeat(node_id, uptime))
            sent += 1
            print(f"[{node_id}] HEARTBEAT  up={uptime}s  ({sent} sent)", flush=True)
            time.sleep(BEACON_S)
    except KeyboardInterrupt:
        pass
    print(f"[{node_id}] beacon stopped after {sent} packet(s)", flush=True)


def run(cfg: dict, radio, source: int | str, preview_port: int | None, loop_file: bool) -> None:
    node_id = cfg["node"]["id"]
    cam_cfg = cfg.get("camera", {})
    width = cam_cfg.get("width", 640)
    height = cam_cfg.get("height", 480)

    # Keys under [detection] map 1:1 onto MotionDetector arguments; anything
    # you leave out keeps the detector's own default.
    detector = MotionDetector(
        width=width,
        height=height,
        fov_h_deg=cam_cfg.get("fov_h_deg", 62.2),
        **cfg.get("detection", {}),
    )

    preview = None
    if preview_port is not None:
        from echinus_node.preview import PreviewServer

        preview = PreviewServer(width, height, port=preview_port)
        print(f"preview at http://<this-pi>:{preview_port}/")

    running = True

    def stop(_sig, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)

    started = time.monotonic()
    last_heartbeat = 0.0
    print(f"[{node_id}] running — Ctrl-C to stop")

    with open_camera(width, height, source) as cam:
        # A video file has a native frame rate; pace replay to it so the
        # detector sees motion at the speed it really happened.
        frame_interval = 1.0 / cam.source_fps if cam.source_fps else 0.0
        next_frame = time.monotonic()

        while running:
            try:
                frame = cam.capture_gray()
            except EndOfStream:
                if not loop_file:
                    print("end of video source")
                    break
                cam.rewind()
                continue

            if preview:
                preview.update_frame(cam.last_frame_bgr)

            result = detector.update(frame)
            if result is not None:
                az, el = result
                radio.send(packets.encode_detect(node_id, int(time.time() * 1000), az, el))
                print(f"[{node_id}] DETECT  az={az:+6.1f}  el={el:+6.1f}")
                if preview and detector.last_centroid:
                    cx, cy = detector.last_centroid
                    preview.add_dot(cx, cy, label=f"az={az:+.1f} el={el:+.1f}")

            now = time.monotonic()
            if now - last_heartbeat > HEARTBEAT_S:
                last_heartbeat = now
                radio.send(packets.encode_heartbeat(node_id, int(now - started)))

            if frame_interval:
                next_frame += frame_interval
                delay = next_frame - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_frame = time.monotonic()  # fell behind; resync

    if preview:
        preview.stop()
    print(f"[{node_id}] stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Echinus node — LoRa motion sensor")
    parser.add_argument("--config", default="node.toml")
    parser.add_argument("--dry-run", action="store_true", help="Skip the radio; print detections only")
    parser.add_argument("--source", default=None,
                        help="Camera index or a video file to replay instead of the live camera")
    parser.add_argument("--loop", action="store_true", help="Restart a video --source when it ends")
    parser.add_argument("--preview", action="store_true", help="Serve an MJPEG view with detection markers")
    parser.add_argument("--preview-port", type=int, default=8080)
    parser.add_argument("--beacon", nargs="?", type=int, const=0, default=None,
                        metavar="COUNT",
                        help="Send heartbeats and nothing else — no camera. Pair it with "
                             "`echinus-hub --listen` to prove the radio link. Optionally "
                             "give a packet count; the default is to keep going")
    args = parser.parse_args()

    try:
        cfg = _load_config(args.config)
    except FileNotFoundError:
        print(f"no config at {args.config} — copy node.toml.example and set the node id", file=sys.stderr)
        sys.exit(1)

    source: int | str = 0
    if args.source is not None:
        source = int(args.source) if args.source.isdigit() else args.source

    radio = _open_radio(cfg, args.dry_run)
    try:
        if args.beacon is not None:
            beacon(cfg, radio, args.beacon or None)  # --beacon with no count: forever
        else:
            run(cfg, radio, source, args.preview_port if args.preview else None, args.loop)
    finally:
        radio.close()


if __name__ == "__main__":
    main()
