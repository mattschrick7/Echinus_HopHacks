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
same frequency, address and RSSI setting as the real thing — which is the
point: a link that works here works for the hub too. Override any of them with
flags.

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

# Mirrors the driver's own tables. It looks these up with .get(value, None) and
# then adds the result to another byte, so a value it doesn't know surfaces as
# a TypeError from inside Waveshare's code rather than as anything readable.
POWER_DBM = (10, 13, 17, 22)
AIR_SPEED_BPS = (1200, 2400, 4800, 9600, 19200, 38400, 62500)
BANDS = ((850, 930), (410, 493))  # floors excluded: the driver tests `freq > x`


def load_lora_config(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f).get("lora", {})


def channel_for(frequency_mhz: int) -> int:
    """MHz - band floor, the same arithmetic the driver and the demo do."""
    for floor, ceiling in BANDS:
        if floor < frequency_mhz <= ceiling:
            return frequency_mhz - floor
    bands = " or ".join(f"{lo + 1}-{hi}MHz" for lo, hi in BANDS)
    sys.exit(f"frequency {frequency_mhz}MHz is outside the driver's range ({bands})")


def build_radio(args):
    import sx126x

    if args.power_dbm not in POWER_DBM:
        sys.exit(f"power_dbm={args.power_dbm} unsupported; pick one of {list(POWER_DBM)}")
    if args.air_speed_bps not in AIR_SPEED_BPS:
        sys.exit(f"air_speed_bps={args.air_speed_bps} unsupported; pick one of {list(AIR_SPEED_BPS)}")
    if not 0 <= args.address <= 0xFFFF:
        sys.exit(f"address={args.address} out of range (0-65535)")

    print(
        f"opening {args.serial_port} @ {args.frequency_mhz}MHz "
        f"(channel {channel_for(args.frequency_mhz)}) addr={args.address} "
        f"air_speed={args.air_speed_bps} rssi={args.rssi}",
        flush=True,
    )
    # Same call shape and argument order as the demo's `node = sx126x.sx126x(...)`.
    return sx126x.sx126x(
        serial_num=args.serial_port,
        freq=args.frequency_mhz,
        addr=args.address,
        power=args.power_dbm,
        rssi=args.rssi,
        air_speed=args.air_speed_bps,
        relay=False,
    )


def check_settings(radio) -> None:
    """Read the module's registers back, because the driver's set() can't fail.

    When the HAT doesn't answer, sx126x.set() prints "setting fail,setting
    again" and carries on with an unconfigured module. This asks the module
    what it is actually holding, using Waveshare's own C1 00 09 query — their
    get_settings() raises NameError, so it can't be called.
    """
    try:
        import RPi.GPIO as GPIO
    except ImportError:
        return

    try:
        GPIO.output(radio.M1, GPIO.HIGH)  # configuration mode
        time.sleep(0.1)
        radio.ser.flushInput()
        radio.ser.write(bytes([0xC1, 0x00, 0x09]))
        time.sleep(0.2)
        reply = bytes(radio.ser.read(radio.ser.inWaiting()))
    finally:
        GPIO.output(radio.M1, GPIO.LOW)  # back to transmission mode
        time.sleep(0.1)
        radio.ser.flushInput()

    if len(reply) < 12 or reply[0] != 0xC1:
        print(f"  WARNING: no answer to the settings query (got {reply.hex(' ') or 'nothing'})", flush=True)
        print("  the HAT is not configured — check the M0/M1 jumpers and the UART", flush=True)
        return

    # cfg_reg and the reply share a layout; the two crypt bytes at the end are
    # write-only and always read back as zero, so they are not compared.
    wrote, got = bytes(radio.cfg_reg[3:10]), reply[3:10]
    if wrote != got:
        print(f"  WARNING: module kept {got.hex(' ')}, we wrote {wrote.hex(' ')}", flush=True)
    else:
        print("  settings readback ok", flush=True)


def do_send(radio, args) -> None:
    """The demo's send_deal(), with the message typed on the command line."""
    offset = channel_for(args.frequency_mhz)
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


def do_listen(radio, args) -> None:
    """The demo's receive loop. radio.receive() prints; it returns nothing."""
    if not args.rssi:
        # Their receive() slices r_buff[3:-1] on the assumption that the last
        # byte is the RSSI the module appends. With rssi off nothing is
        # appended, so the slice eats the final character of the message.
        print("note: rssi is off, so Waveshare's receive() hides the last byte "
              "of each message — pass --rssi for their exact output", flush=True)
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
    parser.add_argument("--rssi", dest="rssi", action="store_true", default=None,
                        help="have the module append a signal-strength byte (the demo's setting)")
    parser.add_argument("--no-rssi", dest="rssi", action="store_false",
                        help="don't append it (what echinus-link uses)")
    parser.add_argument("--dest", type=int, default=None,
                        help="destination address (default: the same address we use)")
    parser.add_argument("--message", default="echinus radio check")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    # Config fills in whatever wasn't given on the command line. The defaults
    # match echinus_link.radio.DEFAULTS, so an unconfigured check still tests
    # the same module settings the node and hub will use.
    cfg = load_lora_config(args.config)
    defaults = {
        "serial_port": "/dev/serial0",
        "frequency_mhz": 915,
        "address": 0,
        "power_dbm": 22,
        "air_speed_bps": 2400,
        "rssi": False,
    }
    for key, fallback in defaults.items():
        if getattr(args, key) is None:
            setattr(args, key, cfg.get(key, fallback))
    if args.dest is None:
        args.dest = cfg.get("destination")

    try:
        radio = build_radio(args)
    except ImportError:
        sys.exit("sx126x not found — run the install script, or shared/fetch-sx126x.sh")
    except SystemExit:
        raise
    except Exception as exc:
        sys.exit(f"could not open the radio: {exc}")

    check_settings(radio)

    if args.mode == "send":
        do_send(radio, args)
    else:
        do_listen(radio, args)


if __name__ == "__main__":
    main()
