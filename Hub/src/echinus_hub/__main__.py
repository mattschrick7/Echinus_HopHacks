"""
Echinus hub — a Raspberry Pi 4 with a LoRa HAT that relays node traffic to the
Server over a websocket.

    LoRa radio  ->  decode  ->  JSON over websocket  ->  Server

That's the entire job. No database, no logic, no state worth backing up.

Run:
    echinus-hub --config hub.toml
    echinus-hub --server ws://192.168.1.50:8000/ws/hub    # override the config
    echinus-hub --listen                                  # print packets, don't relay
    echinus-hub --listen --raw                            # ...and every byte behind them
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import tomllib

from echinus_link import packets

from echinus_hub.relay import Relay


def _load_config(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _describe(burst: bytes) -> str:
    """A burst as hex and as text, for eyeballing traffic that isn't ours.

    Waveshare's demo sends plain strings, and its first three bytes are the
    sender's address and channel rather than payload — so print both halves
    and let the operator recognise what they are looking at.
    """
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in burst)
    if len(burst) >= 3:
        sender = (burst[0] << 8) | burst[1]
        return f"{burst.hex(' ')}\n         from address {sender} channel {burst[2]}: {text[3:]!r}"
    return f"{burst.hex(' ')}  {text!r}"


def _render(msg: dict) -> str:
    """One decoded packet as a line, without the Server's message shape.

    --listen is the tool you reach for when nothing works yet, so it prints
    what actually arrived rather than what the Server would have been told.
    """
    if msg["type"] == "heartbeat":
        return f"HEARTBEAT  {msg['node_id']:<6} seq={msg['seq']:<3} up={msg['uptime_s']}s"
    targets = "  ".join(
        f"T{t['target_id']} az={t['az_deg']:+7.2f} el={t['el_deg']:+7.2f} "
        f"({t['az_rate_dps']:+6.1f},{t['el_rate_dps']:+6.1f})deg/s"
        f"{' coast' if t['coasting'] else ''}"
        for t in msg["targets"]
    )
    return (f"TARGETS    {msg['node_id']:<6} seq={msg['seq']:<3} "
            f"age={msg['age_ms']:>5}ms  {targets}")


def _listen(radio, raw: bool = False) -> None:
    """Field debug: print every packet the radio hears and relay nothing.

    This is also where a deployment's collision problem becomes visible: run
    every node with --beacon, watch the loss counters here, and the answer is
    on screen in under a minute.
    """
    import time

    from echinus_hub.relay import REPORT_EVERY_S, Stats

    print("listening — Ctrl-C to stop", flush=True)
    buffer = b""
    stats = Stats()
    next_report = time.monotonic() + REPORT_EVERY_S
    try:
        while True:
            burst = radio.recv(timeout=1.0)
            if raw and burst:
                print(f"  raw  {_describe(burst)}", flush=True)
            buffer += burst
            found, buffer = packets.extract(buffer, stats.framing)
            for packet in found:
                try:
                    msg = packets.decode(packet)
                except ValueError as exc:
                    print(f"bad packet: {exc}", flush=True)
                    continue
                lost = stats.saw(msg["node_id"], msg["seq"])
                if lost:
                    print(f"lost {lost} packet(s) from {msg['node_id']}", flush=True)
                print(_render(msg), flush=True)

            if time.monotonic() >= next_report:
                next_report = time.monotonic() + REPORT_EVERY_S
                print(stats.summary(), flush=True)
    except KeyboardInterrupt:
        print(f"\n{stats.summary()}")
        print("stopped")


async def _relay(radio, server_url: str, hub_id: str) -> None:
    relay = Relay(radio, server_url, hub_id)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            loop.add_signal_handler(sig, relay.stop)
        except NotImplementedError:
            pass  # Windows dev machines

    print(f"hub '{hub_id}' relaying to {server_url}", flush=True)
    await relay.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Echinus hub — LoRa to websocket relay")
    parser.add_argument("--config", default="hub.toml")
    parser.add_argument("--server", default=None, help="Override the server websocket URL")
    parser.add_argument("--listen", action="store_true", help="Print received packets instead of relaying")
    parser.add_argument("--raw", action="store_true",
                        help="With --listen, also print every byte the radio hears, "
                             "including traffic from Waveshare's own demo")
    args = parser.parse_args()

    try:
        cfg = _load_config(args.config)
    except FileNotFoundError:
        print(f"no config at {args.config} — copy hub.toml.example and set the server URL", file=sys.stderr)
        sys.exit(1)

    from echinus_link.radio import from_config

    radio = from_config(cfg.get("lora", {}))

    if args.listen:
        try:
            _listen(radio, raw=args.raw)
        finally:
            radio.close()  # or the next run finds the serial port still held
        return

    server_url = args.server or cfg["server"]["url"]
    hub_id = cfg.get("hub", {}).get("id", "hub-01")
    try:
        asyncio.run(_relay(radio, server_url, hub_id))
    except KeyboardInterrupt:
        pass
    finally:
        radio.close()
    print("hub stopped")


if __name__ == "__main__":
    main()
