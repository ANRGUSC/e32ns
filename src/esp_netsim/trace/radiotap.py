"""Radiotap + 802.11 frames for Wireshark: ESP-NOW as a vendor action frame (its shape, not
its exact bytes), Wi-Fi as management and data frames (D-016)."""

from __future__ import annotations

import struct

from ..protocol import mac_bytes

ESPRESSIF_OUI = bytes((0x18, 0xFE, 0x34))
_RT_PRESENT = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 5)   # flags, rate, channel, dBm signal
_CHAN_2GHZ_CCK = 0x0080 | 0x0020
_LLC_SNAP = bytes((0xAA, 0xAA, 0x03, 0, 0, 0))


def channel_to_freq_mhz(channel: int) -> int:
    """2.4 GHz channel number to its centre frequency in MHz."""
    return 2412 + (channel - 1) * 5


def build_radiotap(rate_mbps: float, channel: int, rssi_dbm: float) -> bytes:
    """A 15-byte radiotap header: flags, rate, channel, and dBm antenna signal."""
    signal = max(-128, min(127, round(rssi_dbm)))
    # The rate field counts 500 kbps units.
    return struct.pack("<BBHIBBHHb", 0, 0, 15, _RT_PRESENT, 0, round(rate_mbps * 2),
                       channel_to_freq_mhz(channel), _CHAN_2GHZ_CCK, signal)


def _header(control: tuple[int, int], addrs: tuple[bytes, bytes, bytes], seq: int) -> bytes:
    return (struct.pack("<BBH", *control, 0) + b"".join(addrs)
            + struct.pack("<H", (seq & 0x0FFF) << 4))


def build_dot11_action(src_mac: str, dst_mac: str, payload: bytes, seq: int) -> bytes:
    """An 802.11 vendor-specific action frame (Espressif OUI) carrying `payload`."""
    src = mac_bytes(src_mac)
    return _header((0xD0, 0), (mac_bytes(dst_mac), src, src), seq) + bytes((127,)) + ESPRESSIF_OUI + payload


def build_dot11_mgmt(subtype: int, src_mac: str, dst_mac: str, bssid: str, body: bytes,
                     seq: int) -> bytes:
    """An 802.11 management frame (probe, auth, assoc) with its fixed fields and IEs in body."""
    addrs = (mac_bytes(dst_mac), mac_bytes(src_mac), mac_bytes(bssid))
    return _header((subtype << 4, 0), addrs, seq) + body


def build_dot11_data(src_mac: str, dst_mac: str, to_ds: bool, eth_frame: bytes, seq: int) -> bytes:
    """An 802.11 data frame between a station and its AP carrying an Ethernet frame's payload."""
    # ToDS (station to AP) names the final destination; FromDS names the original source.
    addrs = (mac_bytes(dst_mac), mac_bytes(src_mac), eth_frame[0:6] if to_ds else eth_frame[6:12])
    return (_header((0x08, 0x01 if to_ds else 0x02), addrs, seq) + _LLC_SNAP + eth_frame[12:14]
            + eth_frame[14:])
