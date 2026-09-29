"""Radiotap header + ESP-NOW-shaped 802.11 vendor action frame for Wireshark captures (D-016).

A faithful frame *shape*, not a byte-exact ESP-NOW vendor element. Little-endian.
"""

from __future__ import annotations

import struct

from ..protocol import normalize_mac

ESPRESSIF_OUI = bytes((0x18, 0xFE, 0x34))  # Espressif vendor OUI (brief §7)

# radiotap "present" field bits we populate
_RT_FLAGS = 1 << 1
_RT_RATE = 1 << 2
_RT_CHANNEL = 1 << 3
_RT_DBM_ANTSIGNAL = 1 << 5
_RT_PRESENT = _RT_FLAGS | _RT_RATE | _RT_CHANNEL | _RT_DBM_ANTSIGNAL  # 0x2E

# radiotap channel flags: 2 GHz + Dynamic CCK-OFDM is overkill; mark 2GHz + CCK.
_CHAN_FLAG_2GHZ = 0x0080
_CHAN_FLAG_CCK = 0x0020


def channel_to_freq_mhz(channel: int) -> int:
    """2.4 GHz channel number -> centre frequency in MHz (ch1=2412, 5 MHz spacing)."""
    return 2412 + (channel - 1) * 5


def build_radiotap(rate_mbps: float, channel: int, rssi_dbm: float) -> bytes:
    """Build a radiotap header for Flags/Rate/Channel/dBm-antenna-signal.

    Field layout (after the 8-byte it_ header), honoring radiotap alignment:
      off 8  Flags (u8)
      off 9  Rate  (u8, 500 kbps units)
      off 10 Channel: freq u16, flags u16  (2-byte aligned; already even)
      off 14 dBm Antenna Signal (s8)
    Total length = 15 bytes.
    """
    flags = 0x00
    rate = int(round(rate_mbps * 2))  # 500 kbps units: 1 Mbps -> 2
    freq = channel_to_freq_mhz(channel)
    chan_flags = _CHAN_FLAG_2GHZ | _CHAN_FLAG_CCK
    signal = int(round(rssi_dbm))
    signal = max(-128, min(127, signal))

    body = struct.pack("<BB", flags, rate)          # off 8,9
    body += struct.pack("<HH", freq, chan_flags)    # off 10..13
    body += struct.pack("<b", signal)               # off 14
    it_len = 8 + len(body)
    header = struct.pack("<BBH I", 0, 0, it_len, _RT_PRESENT)
    return header + body


def build_dot11_action(src_mac: str, dst_mac: str, payload: bytes,
                       seq: int = 0) -> bytes:
    """Synthesize an 802.11 vendor-specific action frame carrying `payload`.

    Frame Control: type=Management(0), subtype=Action(13) -> 0xD0, flags 0x00.
    Body: category=127 (vendor-specific) + Espressif OUI + payload.
    """
    frame_control = struct.pack("<BB", 0xD0, 0x00)
    duration = struct.pack("<H", 0)
    a1 = bytes.fromhex(normalize_mac(dst_mac).replace(":", ""))  # receiver
    a2 = bytes.fromhex(normalize_mac(src_mac).replace(":", ""))  # transmitter
    a3 = a2                                                       # BSSID ~ src
    seq_ctrl = struct.pack("<H", (seq & 0x0FFF) << 4)
    mac_header = frame_control + duration + a1 + a2 + a3 + seq_ctrl

    category = struct.pack("<B", 127)  # vendor-specific action
    body = category + ESPRESSIF_OUI + payload
    return mac_header + body


def build_frame(src_mac: str, dst_mac: str, payload: bytes, channel: int,
                rate_mbps: float, rssi_dbm: float, seq: int = 0) -> bytes:
    """radiotap header + synthesized 802.11 action frame (the pcapng packet data)."""
    return (build_radiotap(rate_mbps, channel, rssi_dbm)
            + build_dot11_action(src_mac, dst_mac, payload, seq))
