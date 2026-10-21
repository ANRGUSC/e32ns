"""Minimal pcapng writer (SHB/IDB/EPB, little-endian) with radiotap + per-packet comments (D-016)."""

from __future__ import annotations

import struct
from typing import BinaryIO, Optional

from . import radiotap
from ..medium import AP_NODE_ID, Frame

LINKTYPE_IEEE802_11_RADIOTAP = 127

# Block type codes
_BT_SHB = 0x0A0D0D0A
_BT_IDB = 0x00000001
_BT_EPB = 0x00000006

_BYTE_ORDER_MAGIC = 0x1A2B3C4D

# Option codes
_OPT_ENDOFOPT = 0
_OPT_COMMENT = 1
_IDB_OPT_IFNAME = 2
_IDB_OPT_TSRESOL = 9


def _pad32(data: bytes) -> bytes:
    """Pad to a multiple of 4 bytes (pcapng blocks/options are 32-bit aligned)."""
    rem = len(data) % 4
    return data if rem == 0 else data + b"\x00" * (4 - rem)


def _option(code: int, value: bytes) -> bytes:
    return struct.pack("<HH", code, len(value)) + _pad32(value)


def _block(block_type: int, body: bytes) -> bytes:
    total = 12 + len(body)  # type(4) + len(4) + body + len(4)
    return struct.pack("<II", block_type, total) + body + struct.pack("<I", total)


class PcapngWriter:
    """Streaming pcapng writer. One interface, radiotap link type."""

    def __init__(self, fp: BinaryIO, interface_name: str = "esp-netsim"):
        self._fp = fp
        self._write_shb()
        self._write_idb(interface_name)

    # ---- header blocks ------------------------------------------------------
    def _write_shb(self) -> None:
        body = struct.pack("<IHHq", _BYTE_ORDER_MAGIC, 1, 0, -1)  # magic, ver 1.0, seclen -1
        body += _option(_OPT_ENDOFOPT, b"")
        self._fp.write(_block(_BT_SHB, body))

    def _write_idb(self, name: str) -> None:
        body = struct.pack("<HHI", LINKTYPE_IEEE802_11_RADIOTAP, 0, 0)  # linktype, rsvd, snaplen
        # if_tsresol = 6 -> timestamps are in microseconds.
        body += _option(_IDB_OPT_TSRESOL, bytes([6]))
        body += _option(_IDB_OPT_IFNAME, name.encode("utf-8"))
        body += _option(_OPT_ENDOFOPT, b"")
        self._fp.write(_block(_BT_IDB, body))

    # ---- packets ------------------------------------------------------------
    def write_packet(self, ts_us: int, packet_data: bytes,
                     comment: Optional[str] = None) -> None:
        ts = int(ts_us)
        ts_high = (ts >> 32) & 0xFFFFFFFF
        ts_low = ts & 0xFFFFFFFF
        caplen = len(packet_data)
        body = struct.pack("<IIIII", 0, ts_high, ts_low, caplen, caplen)
        body += _pad32(packet_data)
        if comment:
            body += _option(_OPT_COMMENT, comment.encode("utf-8"))
        body += _option(_OPT_ENDOFOPT, b"")
        self._fp.write(_block(_BT_EPB, body))


class TraceRecorder:
    """Writes every medium verdict as a pcapng packet whose comment carries the medium's reasoning."""

    def __init__(self, writer: PcapngWriter):
        self._w = writer

    def on_outcome(self, outcome) -> None:
        f = outcome.frame
        base_comment = (f"tx_status={outcome.status} attempts={outcome.attempts} "
                        f"airtime_us={outcome.airtime_us}")
        if not outcome.receivers:
            # e.g. peer_not_exist / channel_mismatch: record the attempt itself.
            self._w.write_packet(f.start_us + outcome.airtime_us, _packet(f, -100.0),
                                 base_comment + f" reason={outcome.reason}")
            return
        for r in outcome.receivers:
            comment = (base_comment +
                       f" rx_node={r.node_id} delivered={r.delivered} "
                       f"snr_db={r.snr_db:.2f} per={r.per:.3e} reason={r.reason}")
            self._w.write_packet(r.delivery_us, _packet(f, r.rssi_dbm), comment)


def _packet(f: Frame, rssi_dbm: float) -> bytes:
    if f.wifi:
        return (radiotap.build_radiotap(f.rate_mbps, f.channel, rssi_dbm)
                + radiotap.build_dot11_data(f.src_mac, f.dst_mac, f.src_node != AP_NODE_ID,
                                            f.payload, f.seq))
    return radiotap.build_frame(f.src_mac, f.dst_mac, f.payload, f.channel, f.rate_mbps,
                                rssi_dbm, f.seq)
