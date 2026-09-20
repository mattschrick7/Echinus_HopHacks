"""
Echinus node — watch the sky, send what moves over LoRa.

The whole node is this loop:

    capture frame -> MotionDetector -> StreakBuffer -> LoRa packet -> Hub

A node knows exactly one thing about itself: its id. It has no GPS, no
compass, no calibration step and no network config. Where it sits and which
way it points are recorded by the operator on the Server, which is the single
source of truth for all of that.

What the node does *not* do is transmit every frame it sees something. The
radio carries roughly one packet a second; a camera produces fifteen frames in
that time, and several nodes watching one target all produce them at the same
instant. So the node keeps a few frames of history, fits a line to each moving
thing (streaks.py), and transmits only the ones that fit — with their angular
rates, so the Server can work out the frames in between. Filtering here rather
than letting the radio drop packets at random is the difference between
choosing what to lose and having it chosen for you.

Run:
    echinus-node --config node.toml
    echinus-node --config node.toml --dry-run --preview    # no radio, browser view
    echinus-node --config node.toml --beacon               # radio only, no camera
"""
from __future__ import annotations

import argparse
import random
import signal
import sys
import threading
import time
import tomllib
from collections import deque
from typing import Callable

from echinus_link import packets

from echinus_node.camera import EndOfStream, open_camera
from echinus_node.detector import MotionDetector
from echinus_node.streaks import StreakBuffer

HEARTBEAT_S = 60.0       # how often to tell the Server we're alive when nothing moves
HEARTBEAT_JITTER = 0.15  # ±15% on that, so nodes booted together don't phase-lock
BEACON_S = 2.0           # how often --beacon transmits

# [transmit] defaults. `interval_s` is the one that decides airtime: at 2400bps
# a packet is about a tenth of a second on the air, so six nodes sending every
# second is already two thirds of the channel.
TRANSMIT_DEFAULTS = {
    "interval_s": 1.0,       # between packets, once a target is established
    "fast_interval_s": 0.5,  # ...and while one is newly confirmed
    "fast_for_s": 3.0,       # how long "newly confirmed" lasts
    "max_targets": 3,        # targets per packet
    "jitter_s": 0.15,        # random delay before each transmission
    "queue_depth": 4,        # packets held for the radio before we drop the oldest
}


class _DryRunRadio:
    """Stand-in for the LoRa HAT so the node runs on a laptop."""

    def __init__(self, air_speed_bps: int = 2400) -> None:
        self.air_speed_bps = air_speed_bps
        self.sent = 0

    def send(self, data: bytes) -> None:
        self.sent += 1
        print(f"  would transmit {len(data)}B "
              f"({self.estimate_airtime_s(len(data)) * 1000:.0f}ms on air): {data.hex(' ')}")

    def estimate_airtime_s(self, payload_bytes: int) -> float:
        # Same arithmetic as the real Radio, so a dry run reports airtime the
        # deployment would actually spend.
        return (payload_bytes + 3) * 8 / self.air_speed_bps * 1.25

    def close(self) -> None:
        pass


class Transmitter:
    """Hands packets to the radio from a worker thread.

    Three jobs, all of which have to happen off the capture loop:

      * **Don't stall the camera.** Waveshare's send() sleeps 0.2s per call on
        principle, and with listen-before-talk enabled the module can hold the
        line for up to two seconds waiting for a clear channel. Blocking the
        loop on that starves the detector, whose background model and whose
        streak fits both get worse the slower the frames arrive.

      * **Pace consecutive writes by airtime.** The driver hardcodes the UART
        to 9600 baud; at 2400bps air speed the module drains four times slower
        than we can fill it, and overflowing its buffer truncates a packet
        mid-flight. That is corruption with nothing to do with collisions.

      * **Jitter.** Listen-before-talk alone doesn't separate nodes that detect
        the same target on the same frame: they all sense a clear channel at
        the same instant and all transmit. A random delay first is what
        decorrelates them.

    The queue drops the oldest packet when it fills, which is the right way
    round — a stale bearing is worth less than a current one.

    What is queued is a *builder*, not bytes. A packet's `age_ms` has to be
    measured at the instant it goes to the UART, because everything this class
    does — the jitter, the queue, the module deferring for a busy channel — is
    delay between observing and transmitting, and age is the field that cancels
    it. Encoding early and sending late would bake that delay in as error, in
    the one number the Server uses to decide which bearings are simultaneous.
    """

    def __init__(self, radio, depth: int = 4, jitter_s: float = 0.15) -> None:
        self._radio = radio
        self._jitter_s = jitter_s
        self._queue: deque[Callable[[], bytes]] = deque(maxlen=depth)
        self._wake = threading.Event()
        self._running = True
        self.sent = 0
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="tx", daemon=True)
        self._thread.start()

    def offer(self, build: Callable[[], bytes]) -> None:
        """Queue a packet, to be encoded at the moment it is actually sent."""
        if len(self._queue) == self._queue.maxlen:
            self.dropped += 1  # deque discards the oldest for us
        self._queue.append(build)
        self._wake.set()

    def _run(self) -> None:
        while self._running:
            if not self._queue:
                self._wake.wait(0.1)
                self._wake.clear()
                continue
            build = self._queue.popleft()
            if self._jitter_s:
                time.sleep(random.uniform(0.0, self._jitter_s))
            try:
                payload = build()  # now, so age_ms counts the wait above
                self._radio.send(payload)
                self.sent += 1
            except Exception as exc:  # a radio fault must not kill the node
                print(f"transmit failed: {exc}", flush=True)
                continue
            time.sleep(self._radio.estimate_airtime_s(len(payload)))

    def close(self) -> None:
        self._running = False
        self._wake.set()
        self._thread.join(timeout=3.0)


def _build_targets(node_id: str, seq: int, observed_at: float, targets: list):
    """A packet that dates itself when it is sent, not when it is built.

    `observed_at` is a monotonic reading from the frame these fits came from.
    No wall clock is involved anywhere on the node — the Hub supplies the one
    clock in the system, and age is what lets it.
    """
    frozen = list(targets)

    def build() -> bytes:
        age_ms = int((time.monotonic() - observed_at) * 1000)
        return packets.encode_targets(node_id, seq, age_ms, frozen)

    return build


def _load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _open_radio(cfg: dict, dry_run: bool):
    lora = cfg.get("lora", {})
    if dry_run:
        print("dry run — packets are printed, not transmitted")
        return _DryRunRadio(lora.get("air_speed_bps", 2400))
    from echinus_link.radio import from_config

    return from_config(lora)


def _transmit_config(cfg: dict) -> dict:
    tx = dict(TRANSMIT_DEFAULTS)
    tx.update(cfg.get("transmit", {}))
    return tx


def beacon(cfg: dict, radio, count: int | None) -> None:
    """Transmit heartbeats and nothing else, with no camera involved.

    This is the other half of `echinus-hub --listen`: it proves the two radios
    reach each other, in the real packet format, before a camera or a Server is
    in the picture. Waveshare's own demo can read these too — its receiver
    prints the sender address and channel from the three header bytes we send
    ahead of every packet.

    The interval is jittered because this is also how a deployment is bench
    tested: every node beaconing at once is the collision case, and a fixed
    2.0s from nodes started together would phase-lock and hide it.
    """
    node_id = cfg["node"]["id"]
    started = time.monotonic()
    sent = 0
    seq = 0

    print(f"[{node_id}] beacon every ~{BEACON_S:.0f}s — Ctrl-C to stop", flush=True)
    try:
        while count is None or sent < count:
            uptime = int(time.monotonic() - started)
            seq = (seq + 1) & 0xFF
            radio.send(packets.encode_heartbeat(node_id, seq, uptime))
            sent += 1
            print(f"[{node_id}] HEARTBEAT  up={uptime}s  seq={seq}  ({sent} sent)", flush=True)
            time.sleep(BEACON_S * random.uniform(0.8, 1.2))
    except KeyboardInterrupt:
        pass
    print(f"[{node_id}] beacon stopped after {sent} packet(s)", flush=True)


def run(cfg: dict, radio, source: int | str, preview_port: int | None, loop_file: bool) -> None:
    node_id = cfg["node"]["id"]
    cam_cfg = cfg.get("camera", {})
    width = cam_cfg.get("width", 640)
    height = cam_cfg.get("height", 480)
    tx_cfg = _transmit_config(cfg)

    # Keys under [detection] map 1:1 onto MotionDetector arguments, and keys
    # under [tracking] onto StreakBuffer; anything you leave out keeps the
    # default.
    detector = MotionDetector(
        width=width,
        height=height,
        fov_h_deg=cam_cfg.get("fov_h_deg", 62.2),
        **cfg.get("detection", {}),
    )
    streaks = StreakBuffer(**cfg.get("tracking", {}))

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

    transmitter = Transmitter(radio, tx_cfg["queue_depth"], tx_cfg["jitter_s"])
    started = time.monotonic()
    last_heartbeat = started - HEARTBEAT_S  # send one promptly, so the node appears
    heartbeat_due = HEARTBEAT_S
    seq = 0
    next_tx = 0.0
    first_seen: dict[int, float] = {}  # target id -> when we first confirmed it
    print(f"[{node_id}] running — Ctrl-C to stop")

    try:
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

                # The instant these bearings describe. Everything downstream is
                # dated relative to it, and nothing here reads a wall clock —
                # see the note on age_ms in echinus_link.packets.
                now = time.monotonic()
                candidates = detector.candidates(frame)
                streaks.add(int(now * 1000), candidates)

                if preview:
                    for cx, cy in detector.last_centroids:
                        preview.add_dot(cx, cy)

                targets = streaks.confirmed(limit=tx_cfg["max_targets"])
                for target in targets:
                    first_seen.setdefault(target.target_id, now)
                live = {t.target_id for t in targets}
                first_seen = {k: v for k, v in first_seen.items() if k in live}

                if targets and now >= next_tx:
                    # A newly confirmed target is worth more updates: it is the
                    # part of a track the Server knows least about.
                    fresh = any(now - first_seen[t.target_id] < tx_cfg["fast_for_s"]
                                for t in targets)
                    interval = tx_cfg["fast_interval_s"] if fresh else tx_cfg["interval_s"]
                    next_tx = now + interval

                    seq = (seq + 1) & 0xFF
                    # Bound to this frame's instant, and encoded later by the
                    # worker — so age_ms counts the queue and the jitter too.
                    # A coasting target's fit is a few frames older than this;
                    # its `coasting` flag is what says so.
                    transmitter.offer(_build_targets(node_id, seq, now, targets))
                    summary = "  ".join(
                        f"T{t.target_id} az={t.az_deg:+6.1f} el={t.el_deg:+6.1f} "
                        f"({t.az_rate_dps:+.1f},{t.el_rate_dps:+.1f})deg/s"
                        for t in targets
                    )
                    print(f"[{node_id}] seq={seq:<3} {summary}", flush=True)

                if now - last_heartbeat > heartbeat_due:
                    last_heartbeat = now
                    # Re-roll the interval every time, or nodes that started
                    # together stay in step and collide every heartbeat.
                    heartbeat_due = HEARTBEAT_S * random.uniform(
                        1 - HEARTBEAT_JITTER, 1 + HEARTBEAT_JITTER
                    )
                    seq = (seq + 1) & 0xFF
                    payload = packets.encode_heartbeat(node_id, seq, int(now - started))
                    transmitter.offer(lambda p=payload: p)  # nothing time-sensitive in it

                if frame_interval:
                    next_frame += frame_interval
                    delay = next_frame - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_frame = time.monotonic()  # fell behind; resync
    finally:
        transmitter.close()
        if preview:
            preview.stop()

    print(f"[{node_id}] stopped — {transmitter.sent} packet(s) sent, "
          f"{transmitter.dropped} dropped before the radio could take them")


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

    # Check the id before anything expensive opens. It has to fit the packet's
    # 4-byte field, and finding that out from a struct error halfway through a
    # night's watch is not the way to find it out.
    try:
        packets._pack_id(cfg["node"]["id"])
    except (KeyError, ValueError) as exc:
        print(f"bad node id in {args.config}: {exc}", file=sys.stderr)
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
