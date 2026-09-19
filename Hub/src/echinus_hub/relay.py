"""
The relay loop: LoRa in, WebSocket out.

The Hub is deliberately dumb. It decodes each radio packet only far enough to
turn it into JSON, then hands it to the Server. It stores nothing, decides
nothing, and drops nothing on the floor that the Server might have wanted —
if the Server is unreachable, recent packets are held in a small queue and
flushed on reconnect, and the oldest are discarded once it fills.

Keeping it this thin means the Server stays the single source of truth: swap a
Hub for a new one and nothing is lost but the radios' line of sight.
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


class Relay:
    def __init__(self, radio, server_url: str, hub_id: str) -> None:
        self._radio = radio
        self._url = server_url
        self._hub_id = hub_id
        self._queue: deque[dict] = deque(maxlen=QUEUE_MAX)
        self._dropped = 0
        self._running = True

    def stop(self) -> None:
        self._running = False

    # ── radio side ───────────────────────────────────────────────────────────

    def _read_radio(self, buffer: bytes) -> tuple[list[dict], bytes]:
        """One blocking radio read -> decoded messages + undecoded leftover."""
        buffer += self._radio.recv(timeout=RADIO_TIMEOUT_S)
        raw_packets, buffer = packets.extract(buffer)

        messages = []
        for raw in raw_packets:
            try:
                msg = packets.decode(raw)
            except ValueError as exc:
                print(f"bad packet: {exc}", flush=True)
                continue
            msg["hub_id"] = self._hub_id
            msg["received_ms"] = int(time.time() * 1000)
            messages.append(msg)
        return messages, buffer

    async def _receive_forever(self) -> None:
        """Pump the radio into the queue. Blocking reads go to a thread so the
        websocket sender keeps running."""
        buffer = b""
        while self._running:
            messages, buffer = await asyncio.to_thread(self._read_radio, buffer)
            for msg in messages:
                if len(self._queue) == QUEUE_MAX:
                    self._dropped += 1
                self._queue.append(msg)
                print(summarise(msg), flush=True)

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
        return f"DETECT     {msg['node_id']:<12} az={msg['az_deg']:+7.2f} el={msg['el_deg']:+7.2f}"
    return f"HEARTBEAT  {msg['node_id']:<12} up={msg['uptime_s']}s"
