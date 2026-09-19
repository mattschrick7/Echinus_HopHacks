"""
Echinus hub — a Raspberry Pi 4 with a LoRa HAT that relays node traffic to the
Server over a websocket.

    LoRa radio  ->  decode  ->  JSON over websocket  ->  Server

That's the entire job. No database, no logic, no state worth backing up.

Run:
    echinus-hub --config hub.toml
    echinus-hub --server ws://192.168.1.50:8000/ws/hub    # override the config
    echinus-hub --listen                                  # print packets, don't relay
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


def _listen(radio) -> None:
    """Field debug: print every packet the radio hears and relay nothing."""
    from echinus_hub.relay import summarise

    print("listening — Ctrl-C to stop", flush=True)
    buffer = b""
    try:
        while True:
            buffer += radio.recv(timeout=1.0)
            found, buffer = packets.extract(buffer)
            for raw in found:
                try:
                    print(summarise(packets.decode(raw)), flush=True)
                except ValueError as exc:
                    print(f"bad packet: {exc}", flush=True)
    except KeyboardInterrupt:
        print("\nstopped")


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
    args = parser.parse_args()

    try:
        cfg = _load_config(args.config)
    except FileNotFoundError:
        print(f"no config at {args.config} — copy hub.toml.example and set the server URL", file=sys.stderr)
        sys.exit(1)

    from echinus_link.radio import from_config

    radio = from_config(cfg.get("lora", {}))

    if args.listen:
        _listen(radio)
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
