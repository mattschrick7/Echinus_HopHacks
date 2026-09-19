"""The six bytes in front of every transmission, and the settings behind them.

radio.py can't be imported on a machine without the HAT's driver — but
address_header(), channel_for() and the validation table are pure, and they are
the parts that silently break a whole deployment when they're wrong. A bad
header doesn't error, it just transmits to an address nobody holds on a channel
nobody listens to.
"""
from __future__ import annotations

import pytest

from echinus_link import packets
from echinus_link.radio import (
    AIR_SPEED_BPS,
    POWER_DBM,
    Radio,
    address_header,
    channel_for,
)


def test_header_is_destination_then_sender():
    # address 0, 915MHz -> channel 65 (915 - 850)
    assert address_header(0, 65) == bytes([0, 0, 65, 0, 0, 65])


def test_header_splits_a_wide_address():
    assert address_header(0xBEEF, 12) == bytes([0xBE, 0xEF, 12, 0xBE, 0xEF, 12])


def test_broadcast_address():
    assert address_header(0xFFFF, 65)[:2] == b"\xff\xff"


def test_a_separate_destination_goes_in_front_of_our_own_address():
    """Waveshare's demo addresses any node it likes while announcing itself;
    the first three bytes are theirs, the last three ours."""
    header = address_header(7, 65, destination=0xFFFF)

    assert header[:3] == bytes([0xFF, 0xFF, 65])  # eaten by the module
    assert header[3:] == bytes([0, 7, 65])        # travels as payload


@pytest.mark.parametrize("address", [-1, 0x10000])
def test_rejects_impossible_address(address):
    with pytest.raises(ValueError):
        address_header(address, 65)


@pytest.mark.parametrize("destination", [-1, 0x10000])
def test_rejects_impossible_destination(destination):
    with pytest.raises(ValueError):
        address_header(0, 65, destination=destination)


@pytest.mark.parametrize("channel", [-1, 256])
def test_rejects_impossible_channel(channel):
    with pytest.raises(ValueError):
        address_header(0, channel)


def test_receiver_skips_the_header_the_sender_leaves_on_the_air():
    """The last three header bytes travel as payload; extract() must ignore
    them and still find the packet."""
    sent = packets.encode_detect("node-01", 1_700_000_000_000, -8.25, 12.5)
    on_the_air = address_header(0, 65)[3:] + sent  # module eats the first three

    found, leftover = packets.extract(on_the_air)

    assert found == [sent]
    assert leftover == b""
    assert packets.decode(found[0])["node_id"] == "node-01"


def test_a_header_less_packet_is_addressed_to_nonsense():
    """Why this matters: without a header the module reads the packet's own
    first bytes as a destination."""
    sent = packets.encode_detect("node-01", 1, 0.0, 0.0)
    destination = (sent[0] << 8) | sent[1]
    channel = sent[2]

    assert destination == 0xE501        # not any address a deployment holds
    assert channel == ord("n")          # 850 + 110 = 960MHz, not 915


# ── settings the driver can't be trusted to reject ───────────────────────────


@pytest.mark.parametrize("mhz,channel", [(915, 65), (868, 18), (930, 80), (433, 23), (493, 83)])
def test_channel_matches_the_drivers_own_arithmetic(mhz, channel):
    assert channel_for(mhz) == channel


@pytest.mark.parametrize("mhz", [850, 410, 409, 931, 494, 500])
def test_rejects_a_frequency_the_driver_cannot_express(mhz):
    """850 and 410 included: the driver tests `freq > 850` / `elif freq > 410`,
    so 850 is configured as channel 440 and 410 leaves freq_temp undefined."""
    with pytest.raises(ValueError):
        channel_for(mhz)


@pytest.mark.parametrize("power", [0, 20, 25])
def test_rejects_a_power_the_driver_has_no_table_entry_for(power):
    """The driver would do `None + 0x20` and raise TypeError from inside
    Waveshare's code instead."""
    assert power not in POWER_DBM
    with pytest.raises(ValueError, match="power_dbm"):
        Radio(power_dbm=power)


@pytest.mark.parametrize("air_speed", [1000, 2000, 115200])
def test_rejects_an_air_speed_the_driver_has_no_table_entry_for(air_speed):
    assert air_speed not in AIR_SPEED_BPS
    with pytest.raises(ValueError, match="air_speed_bps"):
        Radio(air_speed_bps=air_speed)


def test_rejects_an_impossible_address_before_touching_the_hardware():
    with pytest.raises(ValueError, match="address"):
        Radio(address=70000)
