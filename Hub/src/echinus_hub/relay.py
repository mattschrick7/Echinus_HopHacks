"""
The relay loop: LoRa in, WebSocket out.

The Hub is deliberately dumb. It decodes each radio packet only far enough to
turn it into JSON, then hands it to the Server. It stores nothing, decides
nothing, and drops nothing on the floor that the Server might have wanted —
if the Server is unreachable, recent packets are held in a small queue and
flushed on reconnect, and the oldest are discarded once it fills.

Keeping it this thin means the Server stays the single source of truth: swap a
Hub for a new one and nothing is lost but the radios' line of sight.

Two jobs beyond relaying, both of which have to live here because nowhere else
can see them:

  * **Expanding a TARGETS packet.** A node transmits a line fit per target —
    a bearing and an angular rate. The Server's ingest takes one `detect` per
    bearing, so one packet becomes one message per target. The rates ride
    along for the Server to use when it is ready to; nothing downstream has to
    change for the rest of this to work.

  * **Counting what went missing.** Packets carry a per-node sequence number,
    so a gap in it is a lost packet. Without this the system cannot tell a
    dropped transmission from a camera with nothing to report, which makes
    every radio setting in the deployment unmeasurable. See Stats.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque

import websockets

from echinus_link import packets

RECONNECT_S = 5.0        # wait between reconnect attempts
QUEUE_MAX = 5000         # packets held while the Server is unreachable
RADIO_TIMEOUT_S = 1.0    # how long a single radio read blocks
REPORT_EVERY_S = 60.0    # how often to print the link summary


class Stats:
    """Per-node packet accounting, and what the framing threw away.

    Loss is inferred from gaps in the one-byte sequence number. The counter
    wraps at 256, so a gap is only meaningful below that — a node that goes
    away for long enough to wrap is counted as one loss, not 300. That is the
    right way to be wrong here: this number exists to answer "are we losing
    packets right now", and a reboot or a long outage shouldn't swamp it.
    """

    def __init__(self) -> None:
        self.received: dict[str, int] = {}
        self.lost: dict[str, int] = {}
        self.last_seq: dict[str, int] = {}
        self.framing = packets.FramingStats()

    def saw(self, node_id: str, seq: int) -> int:
        """Record a packet; return how many appear to have been lost before it."""
        self.received[node_id] = self.received.get(node_id, 0) + 1

        previous = self.last_seq.get(node_id)
        self.last_seq[node_id] = seq
        if previous is None:
            return 0  # first packet from this node: nothing to compare against

        gap = (seq - previous - 1) % 256
        if gap:
            # A very large gap is a node that restarted or was away a long
            # while, not 200 individual losses.
            gap = 1 if gap > 64 else gap
            self.lost[node_id] = self.lost.get(node_id, 0) + gap
        return gap

    def summary(self) -> str:
        if not self.received:
            line = "no packets yet"
        else:
            line = "  ".join(
                f"{node}: {self.received[node]} rx, {self.lost.get(node, 0)} lost "
                f"({self._rate(node):.0%})"
                for node in sorted(self.received)
            )
        return (
            f"link  {line}  |  {self.framing.crc_errors} CRC fail, "
            f"{self.framing.skipped_bytes} stray bytes"
        )

    def _rate(self, node: str) -> float:
        lost = self.lost.get(node, 0)
        total = self.received[node] + lost
        return lost / total if total else 0.0


class Relay:
    def __init__(self, radio, server_url: str, hub_id: str) -> None:
        self._radio = radio
        self._url = server_url
        self._hub_id = hub_id
        self._queue: deque[dict] = deque(maxlen=QUEUE_MAX)
        self._dropped = 0
        self._running = True
        self.stats = Stats()

    def stop(self) -> None:
        self._running = False

    # ── radio side ───────────────────────────────────────────────────────────

    def _read_radio(self, buffer: bytes) -> tuple[list[dict], bytes]:
        """One blocking radio read -> decoded messages + undecoded leftover."""
        buffer += self._radio.recv(timeout=RADIO_TIMEOUT_S)
        raw_packets, buffer = packets.extract(buffer, self.stats.framing)

        messages = []
        for raw in raw_packets:
            try:
                msg = packets.decode(raw)
            except ValueError as exc:
                # extract() has already checked the CRC, so this is a packet
                # that is intact but not one we understand — a newer node build,
                # most likely.
                print(f"bad packet: {exc}", flush=True)
                continue
            messages.extend(self._expand(msg))
        return messages, buffer

    def _expand(self, msg: dict) -> list[dict]:
        """One decoded packet -> the messages the Server expects.

        A heartbeat is one message. A TARGETS packet is one `detect` per
        target.

        This is where a packet gets its time. Nodes send no clock reading at
        all, only how long ago they saw what they are reporting, so every
        detection in the deployment is dated on this one hub clock — no NTP on
        the nodes, and no class of failure where two nodes watch the same drone
        and the Server never pairs them because their clocks disagree.

        `seq` and `target_id` travel through for the Server to deduplicate on:
        with two hubs in earshot, each computes its own arrival time, so the
        timestamps of one node's packet no longer match between them.
        """
        received_ms = int(time.time() * 1000)
        node_id = msg["node_id"]
        seq = msg["seq"]
        lost = self.stats.saw(node_id, seq)
        if lost:
            print(f"lost {lost} packet(s) from {node_id}", flush=True)

        common = {"node_id": node_id, "hub_id": self._hub_id,
                  "received_ms": received_ms, "seq": seq}

        if msg["type"] == "heartbeat":
            return [{**common, "type": "heartbeat", "uptime_s": msg["uptime_s"]}]

        timestamp_ms = received_ms - msg["age_ms"]
        return [
            {
                **common,
                "type": "detect",
                "timestamp_ms": timestamp_ms,
                "az_deg": t["az_deg"],
                "el_deg": t["el_deg"],
                # Carried for the Server to use when it tracks per-node
                # targets; harmless to ignore until then.
                "target_id": t["target_id"],
                "az_rate_dps": t["az_rate_dps"],
                "el_rate_dps": t["el_rate_dps"],
                "coasting": t["coasting"],
            }
            for t in msg["targets"]
        ]

    async def _receive_forever(self) -> None:
        """Pump the radio into the queue. Blocking reads go to a thread so the
        websocket sender keeps running."""
        buffer = b""
        next_report = time.monotonic() + REPORT_EVERY_S
        while self._running:
            messages, buffer = await asyncio.to_thread(self._read_radio, buffer)
            for msg in messages:
                if len(self._queue) == QUEUE_MAX:
                    self._dropped += 1
                self._queue.append(msg)
                print(summarise(msg), flush=True)

            if time.monotonic() >= next_report:
                next_report = time.monotonic() + REPORT_EVERY_S
                print(self.stats.summary(), flush=True)

    # ── server side ──────────────────────────────────────────────────────────

    async def _send_forever(self) -> None:
        """Keep one websocket to the Server open and drain the queue into it."""
        while self._running:
            try:
                async with websockets.connect(self._url, ping_interval=20) as ws:
                    print(f"connected to {self._url}", flush=True)
                    await ws.send(json.dumps({"type": "hello", "hub_id": self._hub_id}))
                    if self._dropped:
                        print(f"dropped {self._dropped} packet(s) while offline", flush=True)
                        self._dropped = 0
                    await self._drain(ws)
            except Exception as exc:  # any disconnect: log, wait, try again
                if self._running:
                    print(f"server unreachable ({exc}) — retrying in {RECONNECT_S:.0f}s", flush=True)
                    await asyncio.sleep(RECONNECT_S)

    async def _drain(self, ws) -> None:
        while self._running:
            if not self._queue:
                await asyncio.sleep(0.05)
                continue
            msg = self._queue[0]
            await ws.send(json.dumps(msg))
            self._queue.popleft()  # only once the send succeeded

    async def run(self) -> None:
        await asyncio.gather(self._receive_forever(), self._send_forever())


def summarise(msg: dict) -> str:
    if msg["type"] == "detect":
        coast = " coast" if msg.get("coasting") else ""
        return (
            f"DETECT     {msg['node_id']:<6} T{msg.get('target_id', 0):<3} "
            f"az={msg['az_deg']:+7.2f} el={msg['el_deg']:+7.2f} "
            f"({msg.get('az_rate_dps', 0.0):+6.1f},{msg.get('el_rate_dps', 0.0):+6.1f})deg/s{coast}"
        )
    return f"HEARTBEAT  {msg['node_id']:<6} up={msg['uptime_s']}s"
