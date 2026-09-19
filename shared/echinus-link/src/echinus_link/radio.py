"""
Waveshare SX1262 LoRa HAT, wrapped in a tiny send/receive object.

Both the Node (Pi Zero, transmits) and the Hub (Pi 4, receives) use the same
HAT and therefore the same class — a Node only ever calls send(), a Hub only
ever calls recv().

Needs Waveshare's `sx126x.py` driver, which is not on PyPI. Copy it from the
Waveshare SX126x HAT demo repository onto the Pi's PYTHONPATH (the install
scripts in Node/deploy and Hub/deploy do this for you).

Every radio in one deployment must agree on frequency, address and air speed.
"""
from __future__ import annotations

import time

_POLL_S = 0.02        # how often recv() checks the UART for the first byte
_BURST_SETTLE_S = 0.2  # once bytes start arriving, wait this long for the rest

# Defaults — override per deployment in node.toml / hub.toml.
DEFAULTS = {
    "serial_port": "/dev/ttyS0",
    "frequency_mhz": 915,
    "address": 0,
    "power_dbm": 22,
    "air_speed_bps": 2400,
}


class Radio:
    """Raw bytes in, raw bytes out. Packet framing is packets.extract()'s job."""

    def __init__(
        self,
        serial_port: str = DEFAULTS["serial_port"],
        frequency_mhz: int = DEFAULTS["frequency_mhz"],
        address: int = DEFAULTS["address"],
        power_dbm: int = DEFAULTS["power_dbm"],
        air_speed_bps: int = DEFAULTS["air_speed_bps"],
    ) -> None:
        try:
            import sx126x
        except ImportError as exc:
            raise RuntimeError(
                "sx126x driver not found — copy sx126x.py from the Waveshare "
                "SX126x HAT demo repository next to your code or onto PYTHONPATH"
            ) from exc

        self._hat = sx126x.sx126x(
            serial_num=serial_port,
            freq=frequency_mhz,
            addr=address,
            power=power_dbm,
            rssi=False,
            air_speed=air_speed_bps,
            relay=False,
        )
        # Waveshare's own receive() prints to stdout instead of returning data,
        # so recv() below reads the driver's serial port directly.
        self._serial = getattr(self._hat, "ser", None)

    def send(self, data: bytes) -> None:
        self._hat.send(data)

    def recv(self, timeout: float = 1.0) -> bytes:
        """One radio burst of raw bytes, or b"" if nothing arrived in `timeout`."""
        if self._serial is None:
            raise RuntimeError("this sx126x driver build does not expose .ser — cannot receive")

        deadline = time.monotonic() + timeout
        while self._serial.inWaiting() == 0:
            if time.monotonic() >= deadline:
                return b""
            time.sleep(_POLL_S)

        time.sleep(_BURST_SETTLE_S)  # let the rest of the burst land in the buffer
        return bytes(self._serial.read(self._serial.inWaiting()))

    def close(self) -> None:
        pass  # the Waveshare driver has no close()


def from_config(cfg: dict) -> Radio:
    """Build a Radio from a [lora] config table; every key is optional."""
    return Radio(**{key: cfg.get(key, default) for key, default in DEFAULTS.items()})
