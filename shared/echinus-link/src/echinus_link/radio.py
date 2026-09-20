"""
Waveshare SX1262 LoRa HAT, wrapped in a tiny send/receive object.

Both the Node (Pi Zero, transmits) and the Hub (Pi 4, receives) use the same
HAT and therefore the same class — a Node only ever calls send(), a Hub only
ever calls recv().

Needs Waveshare's `sx126x.py` driver, which is not on PyPI. Copy it from the
Waveshare SX126x HAT demo repository onto the Pi's PYTHONPATH (the install
scripts in Node/deploy and Hub/deploy do this for you).

Every radio in one deployment must agree on frequency, address and air speed.

Three things the HAT's firmware and Waveshare's driver impose on us:

  * The module runs in fixed-point transmission mode — the driver hard-codes
    it, register 3 bit 6, via `cfg_reg[9] = 0x43 + rssi`. The first three bytes
    handed to the module are therefore not payload but a destination: address
    high, address low, channel. Miss that and the module happily transmits your
    packet's own first bytes as an address, to nobody, on whatever channel byte
    three happened to be. See address_header().

  * Frequency, power and air speed are looked up in the driver's own tables
    with `.get(value, None)`, and the result is then added to another byte. A
    value outside those tables doesn't raise anything legible — it surfaces as
    a TypeError from inside Waveshare's code, or configures a channel that
    doesn't exist. See _validate() and channel_for().

  * `set()` cannot fail. If the module never answers, the driver prints
    "setting fail,setting again" to stdout, gives up, and returns — leaving an
    unconfigured module that transmits into the void. Its own get_settings()
    can't be used to check (it references an undefined `M1` and unqualified
    dict names, so it raises NameError), so check_module_config() below speaks
    the same C1 00 09 query itself.
"""
from __future__ import annotations

import time

_POLL_S = 0.02          # how often recv() checks the UART for the first byte
_BURST_SETTLE_S = 0.15  # once bytes start arriving, wait this long for the rest.
                        # Waveshare's own receive() waits 0.5s; our packets are
                        # tens of bytes, so that was mostly added latency.

# Register 3 (cfg_reg[9]), bit 4: listen before talk. The module samples the
# channel and defers if it hears traffic, for up to two seconds before sending
# anyway. Waveshare's driver never sets it — see _enable_lbt().
_LBT_BIT = 0x10

# LoRa spends more time on air than the payload's bit count suggests: preamble,
# sync word, header, its own CRC. A flat 25% is crude but close enough to pace
# transmissions with, which is all estimate_airtime_s() is for.
_AIRTIME_OVERHEAD = 1.25

# The module consumes the first three bytes of what we hand it as a
# destination; they never go on the air. See address_header().
_HEADER_BYTES_EATEN = 3

# Values the driver's lora_power_dic / lora_air_speed_dic accept. Anything else
# becomes None, and then `None + 0x20`.
POWER_DBM = (10, 13, 17, 22)
AIR_SPEED_BPS = (1200, 2400, 4800, 9600, 19200, 38400, 62500)

# The driver chooses a band with `if freq > 850 ... elif freq > 410`, so both
# band floors are unusable: 850 falls into the low branch and is configured as
# channel 440, and 410 matches neither branch, leaving freq_temp undefined.
# The ranges below are therefore exclusive at the bottom, inclusive at the top.
BANDS = ((850, 930), (410, 493))  # E22-900T22S, E22-400T22S

# Everyone hears a packet addressed here, whatever their own address.
BROADCAST = 0xFFFF

# Defaults — override per deployment in node.toml / hub.toml.
DEFAULTS = {
    "serial_port": "/dev/serial0",
    "frequency_mhz": 915,
    "address": 0,
    "destination": None,   # None: address the address we hold ourselves
    "power_dbm": 22,
    "air_speed_bps": 2400,
    "rssi": False,
    "lbt": True,
}


def channel_for(frequency_mhz: int) -> int:
    """The channel byte the driver derives from a frequency: MHz - band floor.

    Raises ValueError for a frequency the driver can't express — which is not
    quite the same as one the module can't reach; see BANDS.
    """
    for floor, ceiling in BANDS:
        if floor < frequency_mhz <= ceiling:
            return frequency_mhz - floor
    bands = " or ".join(f"{lo + 1}-{hi}MHz" for lo, hi in BANDS)
    raise ValueError(
        f"frequency_mhz={frequency_mhz} is outside what Waveshare's driver can "
        f"configure ({bands}); the band floors themselves are excluded"
    )


def _validate(address: int, destination: int, power_dbm: int, air_speed_bps: int) -> None:
    for name, value in (("address", address), ("destination", destination)):
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"{name} out of range: {value} (0-65535)")
    if power_dbm not in POWER_DBM:
        raise ValueError(f"power_dbm={power_dbm} unsupported; pick one of {list(POWER_DBM)}")
    if air_speed_bps not in AIR_SPEED_BPS:
        raise ValueError(
            f"air_speed_bps={air_speed_bps} unsupported; pick one of {list(AIR_SPEED_BPS)}"
        )


def address_header(address: int, channel: int, destination: int | None = None) -> bytes:
    """The six bytes Waveshare's firmware expects in front of a payload.

    The first three are consumed by the module as the destination (address
    high, address low, channel) and never go on the air. The last three are
    ordinary payload that Waveshare's demo receiver interprets as the sender's
    own address and channel; we keep the convention so their tools can read our
    traffic, and packets.extract() skips them by scanning for MAGIC.

    This is byte-for-byte the layout in the demo's send_deal(). `address` is
    ours; `destination` is whose radio should hear it, defaulting to our own
    address because one deployment normally shares one.

    Channel is the frequency offset the driver computes: MHz - 850 on the
    high band, MHz - 410 on the low one.
    """
    if destination is None:
        destination = address
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"address out of range: {address}")
    if not 0 <= destination <= 0xFFFF:
        raise ValueError(f"destination out of range: {destination}")
    if not 0 <= channel <= 0xFF:
        raise ValueError(f"channel out of range: {channel}")
    return bytes([
        destination >> 8, destination & 0xFF, channel,
        address >> 8, address & 0xFF, channel,
    ])


class Radio:
    """Raw bytes in, raw bytes out. Packet framing is packets.extract()'s job."""

    def __init__(
        self,
        serial_port: str = DEFAULTS["serial_port"],
        frequency_mhz: int = DEFAULTS["frequency_mhz"],
        address: int = DEFAULTS["address"],
        destination: int | None = DEFAULTS["destination"],
        power_dbm: int = DEFAULTS["power_dbm"],
        air_speed_bps: int = DEFAULTS["air_speed_bps"],
        rssi: bool = DEFAULTS["rssi"],
        lbt: bool = DEFAULTS["lbt"],
        verify: bool = True,
    ) -> None:
        # Everything the driver would fail on obscurely, checked while we can
        # still say which setting is wrong.
        if destination is None:
            destination = address
        _validate(address, destination, power_dbm, air_speed_bps)
        channel = channel_for(frequency_mhz)

        try:
            import sx126x
        except ImportError as exc:
            raise RuntimeError(
                "sx126x driver not found — copy sx126x.py from the Waveshare "
                "SX126x HAT demo repository next to your code or onto PYTHONPATH"
            ) from exc

        # Same call shape as the demo's main.py, which is the only arrangement
        # of these arguments Waveshare actually tests.
        self._hat = sx126x.sx126x(
            serial_num=serial_port,
            freq=frequency_mhz,
            addr=address,
            power=power_dbm,
            rssi=rssi,
            air_speed=air_speed_bps,
            relay=False,
        )
        # Waveshare's own receive() prints to stdout instead of returning data,
        # so recv() below reads the driver's serial port directly.
        self._serial = getattr(self._hat, "ser", None)

        # The driver recomputes the channel itself. If we and it ever disagree,
        # every header we send goes out addressed to the wrong channel.
        if self._hat.offset_freq != channel:
            raise RuntimeError(
                f"channel mismatch: we make {frequency_mhz}MHz channel {channel}, "
                f"the installed sx126x driver makes it {self._hat.offset_freq}"
            )

        self.address = address
        self.destination = destination
        self.channel = channel
        self.air_speed_bps = air_speed_bps
        self.lbt = lbt
        self._header = address_header(address, channel, destination)

        if lbt and not self._enable_lbt():
            print(
                "radio: the HAT did not accept listen-before-talk — transmissions "
                "will not defer for each other",
                flush=True,
            )

        if verify:
            ok, detail = self.check_module_config()
            print(f"radio: {detail}", flush=True)
            if not ok:
                print(
                    "  the HAT did not take its settings — nothing will be sent or\n"
                    "  received. Check that the M0/M1 jumpers are REMOVED, that the\n"
                    "  serial console is off and the UART on (raspi-config), and that\n"
                    "  nothing else holds the serial port.",
                    flush=True,
                )

    def _enable_lbt(self) -> bool:
        """Turn on the module's listen-before-talk. The driver never does.

        Without it, several nodes watching one target transmit at exactly the
        same instant — which is not an edge case here but the design case, since
        triangulation *requires* two or more nodes seeing the same thing at
        once. With it, the module samples the channel first and defers.

        The driver has no argument for this, so we set the bit in its own
        cfg_reg and write the registers ourselves, using the same M1-high
        configuration mode check_module_config() uses to read them. Because the
        bit lives in cfg_reg, the readback there then verifies it for free.

        Returns False rather than raising: a deployment with no LBT still
        works, just worse, and the caller is better placed to decide.
        """
        try:
            import RPi.GPIO as GPIO
        except ImportError:
            return True  # not on a Pi; nothing to configure and nothing to warn about
        if self._serial is None:
            return True

        self._hat.cfg_reg[9] |= _LBT_BIT
        try:
            GPIO.output(self._hat.M1, GPIO.HIGH)  # configuration mode
            time.sleep(0.1)
            self._serial.flushInput()
            self._serial.write(bytes(self._hat.cfg_reg))
            time.sleep(0.2)
            reply = bytes(self._serial.read(self._serial.inWaiting()))
        finally:
            GPIO.output(self._hat.M1, GPIO.LOW)  # back to transmission mode
            time.sleep(0.1)
            self._serial.flushInput()  # drop the reply, so recv() never sees it

        return len(reply) >= 12 and reply[0] == 0xC1

    def estimate_airtime_s(self, payload_bytes: int) -> float:
        """Roughly how long `payload_bytes` will occupy the channel.

        Used to pace consecutive sends. This matters more than it looks: the
        driver hardcodes the UART to 9600 baud, and at the default 2400 bps air
        speed the module drains four times slower than we can fill it. Writing
        back-to-back overflows its buffer and truncates a packet mid-flight,
        which is corruption that has nothing to do with collisions and happens
        with a single node running alone.
        """
        on_air = payload_bytes + len(self._header) - _HEADER_BYTES_EATEN
        return on_air * 8 / self.air_speed_bps * _AIRTIME_OVERHEAD

    def send(self, data: bytes) -> None:
        self._hat.send(self._header + data)

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

    def check_module_config(self) -> tuple[bool, str]:
        """Ask the module what settings it is actually holding.

        Waveshare's protocol: pull M1 high for configuration mode, send
        C1 00 09 ("read 9 registers starting at 0"), and the module answers
        with C1 00 09 followed by those nine bytes — the same nine the driver
        wrote in set().

        Returns (ok, one line worth printing). Never raises: a module that
        failed to configure is useless, but so is a crash, and the caller is
        better placed to decide which.
        """
        try:
            import RPi.GPIO as GPIO
        except ImportError:
            return True, "settings readback skipped (no RPi.GPIO)"
        if self._serial is None:
            return True, "settings readback skipped (driver exposes no .ser)"

        try:
            GPIO.output(self._hat.M1, GPIO.HIGH)  # configuration mode
            time.sleep(0.1)
            self._serial.flushInput()
            self._serial.write(bytes([0xC1, 0x00, 0x09]))
            time.sleep(0.2)
            reply = bytes(self._serial.read(self._serial.inWaiting()))
        finally:
            GPIO.output(self._hat.M1, GPIO.LOW)  # back to transmission mode
            time.sleep(0.1)
            self._serial.flushInput()  # drop the reply, so recv() never sees it

        if len(reply) < 12 or reply[0] != 0xC1:
            return False, f"module did not answer the settings query (got {reply.hex(' ') or 'nothing'})"

        # cfg_reg is [C2, start, len, ADDH, ADDL, NETID, REG0, REG1, REG2,
        # REG3, CRYPT_H, CRYPT_L] and the reply has the same layout. Compare
        # every register except the two crypt ones, which are write-only and
        # always read back as zero.
        wrote, got = bytes(self._hat.cfg_reg[3:10]), reply[3:10]
        if wrote != got:
            return False, f"module kept {got.hex(' ')}, we wrote {wrote.hex(' ')}"

        return True, (
            f"configured — address {self.address}, channel {self.channel} "
            f"({self.channel + self._hat.start_freq}.125MHz), addressing {self.destination}, "
            f"{self.air_speed_bps}bps air, "
            f"listen-before-talk {'on' if got[6] & _LBT_BIT else 'OFF'}"
        )

    def close(self) -> None:
        # The driver has no close(); releasing the port is still ours to do, so
        # a restart doesn't trip over a port the last process left open.
        if self._serial is not None and self._serial.is_open:
            self._serial.close()


def from_config(cfg: dict) -> Radio:
    """Build a Radio from a [lora] config table; every key is optional."""
    return Radio(**{key: cfg.get(key, default) for key, default in DEFAULTS.items()})
