"""Talk to the LoRa HAT the way Waveshare's own demo does, and nothing else.

This deliberately bypasses echinus-link: no Radio wrapper, no packet format,
no framing. It constructs sx126x exactly as SX126X_LoRa_HAT_Code/raspberrypi/
python/main.py does and uses the driver's own send()/receive(). If this works
and the node/hub don't, the fault is in our code. If this doesn't work either,
the fault is in wiring, jumpers, antennas or radio settings.

Run it on both Pis, receiver first:

    uv run python shared/radio_check.py listen --config Hub/hub.toml
    uv run python shared/radio_check.py send   --config Node/node.toml

Settings come from a [lora] table if you pass --config, so the test uses the
same frequency and address as the real thing. Override any of them with flags.

Remember the HAT's own requirements, from Waveshare's demo:
  - the M0 and M1 jumpers must be REMOVED when the HAT is on a Pi
  - the serial login shell must be off and the UART on (raspi-config)
  - an antenna must be attached before transmitting at 22dBm
"""
from __future__ import annotations

import argparse
import sys
import time
import tomllib


def load_lora_config(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f).get("lora", {})


def build_radio(args):
    import sx126x

    print(
        f"opening {args.serial_port} @ {args.frequency_mhz}MHz "
        f"addr={args.address} air_speed={args.air_speed_bps}",
        flush=True,
    )
    # Same call shape and argument order as the demo's `node = sx126x.sx126x(...)`.
    return sx126x.sx126x(
        serial_num=args.serial_port,
        freq=args.frequency_mhz,
        addr=args.address,
        power=args.power_dbm,
        rssi=True,          # the demo enables this; it prints signal strength
        air_speed=args.air_speed_bps,
        relay=False,
    )


def do_send(radio, args) -> None:
    """The demo's send_deal(), with the message typed on the command line."""
    # offset_frequence = MHz - 850 on the high band, - 410 on the low one.
    offset = args.frequency_mhz - (850 if args.frequency_mhz > 850 else 410)
    dest = args.dest if args.dest is not None else args.address

    for i in range(args.count):
        payload = f"{args.message} {i + 1}".encode()
        # Byte-for-byte the demo's layout:
        #   dest addr hi, dest addr lo, dest channel,
        #   own addr hi,  own addr lo,  own channel, payload
        data = (
            bytes([dest >> 8])
            + bytes([dest & 0xFF])
            + bytes([offset])
            + bytes([radio.addr >> 8])
            + bytes([radio.addr & 0xFF])
            + bytes([radio.offset_freq])
            + payload
        )
        radio.send(data)
        print(f"sent -> addr={dest} channel={offset}: {payload.decode()}", flush=True)
        time.sleep(args.interval)

    print("done — anything received? check the listener", flush=True)


def do_listen(radio) -> None:
    """The demo's receive loop. radio.receive() prints; it returns nothing."""
    print("listening — Ctrl-C to stop", flush=True)
    try:
        while True:
            radio.receive()
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nstopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["send", "listen"])
    parser.add_argument("--config", default=None, help="read [lora] from this TOML")
    parser.add_argument("--serial-port")
    parser.add_argument("--frequency-mhz", type=int)
    parser.add_argument("--address", type=int)
    parser.add_argument("--power-dbm", type=int)
    parser.add_argument("--air-speed-bps", type=int)
    parser.add_argument("--dest", type=int, default=None,
                        help="destination address (default: the same address we use)")
    parser.add_argument("--message", default="echinus radio check")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    # Config fills in whatever wasn't given on the command line.
    cfg = load_lora_config(args.config)
    defaults = {
        "serial_port": "/dev/serial0",
        "frequency_mhz": 915,
        "address": 0,
        "power_dbm": 22,
        "air_speed_bps": 2400,
    }
    for key, fallback in defaults.items():
        if getattr(args, key) is None:
            setattr(args, key, cfg.get(key, fallback))

    try:
        radio = build_radio(args)
    except ImportError:
        sys.exit("sx126x not found — run the install script, or shared/fetch-sx126x.sh")
    except Exception as exc:
        sys.exit(f"could not open the radio: {exc}")

    if args.mode == "send":
        do_send(radio, args)
    else:
        do_listen(radio)


if __name__ == "__main__":
    main()
