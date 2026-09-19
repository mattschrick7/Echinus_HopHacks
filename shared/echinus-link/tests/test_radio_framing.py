"""The six bytes in front of every transmission.

radio.py can't be imported on a machine without the HAT's driver, but
address_header() is pure and is the part that silently breaks a whole
deployment when it's wrong — a bad header doesn't error, it just transmits to
an address nobody holds on a channel nobody listens to.
"""
from __future__ import annotations

import pytest

from echinus_link import packets
from echinus_link.radio import address_header


def test_header_is_destination_then_sender():
    # address 0, 915MHz -> channel 65 (915 - 850)
    assert address_header(0, 65) == bytes([0, 0, 65, 0, 0, 65])


def test_header_splits_a_wide_address():
    assert address_header(0xBEEF, 12) == bytes([0xBE, 0xEF, 12, 0xBE, 0xEF, 12])


def test_broadcast_address():
    assert address_header(0xFFFF, 65)[:2] == b"\xff\xff"


@pytest.mark.parametrize("address", [-1, 0x10000])
def test_rejects_impossible_address(address):
    with pytest.raises(ValueError):
        address_header(address, 65)


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
