"""Minimal pcapng writer (SHB/IDB/EPB, little-endian) with radiotap and per-packet comments (D-016)."""

from __future__ import annotations

import struct
from typing import BinaryIO, Callable

from . import radiotap
from ..medium import AP_NODE_ID, Frame, TxOutcome

LINKTYPE_IEEE802_11_RADIOTAP = 127
_SHB, _IDB, _EPB = 0x0A0D0D0A, 0x00000001, 0x00000006
_OPT_END, _OPT_COMMENT, _OPT_IFNAME, _OPT_TSRESOL = 0, 1, 2, 9


def _pad32(data: bytes) -> bytes:
    return data + bytes(-len(data) % 4)


def _option(code: int, value: bytes) -> bytes:
    return struct.pack("<HH", code, len(value)) + _pad32(value)


def _block(block_type: int, body: bytes) -> bytes:
    total = 12 + len(body)  # type, length, body, length again
    return struct.pack("<II", block_type, total) + body + struct.pack("<I", total)


class PcapngWriter:
    """Streaming pcapng writer: one radiotap interface with microsecond timestamps."""

    def __init__(self, fp: BinaryIO, interface_name: str):
        self._fp = fp
        end = _option(_OPT_END, b"")
        # Byte-order magic, version 1.0, section length unknown.
        fp.write(_block(_SHB, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1) + end))
        fp.write(_block(_IDB, struct.pack("<HHI", LINKTYPE_IEEE802_11_RADIOTAP, 0, 0)
                        + _option(_OPT_TSRESOL, bytes([6])) + _option(_OPT_IFNAME, interface_name.encode())
                        + end))

    def write_packet(self, ts_us: int, packet: bytes, comment: str) -> None:
        body = struct.pack("<IIIII", 0, ts_us >> 32, ts_us & 0xFFFFFFFF, len(packet), len(packet))
        body += _pad32(packet) + _option(_OPT_COMMENT, comment.encode()) + _option(_OPT_END, b"")
        self._fp.write(_block(_EPB, body))


def trace_hook(writer: PcapngWriter) -> Callable[[TxOutcome], None]:
    """An outcome hook that writes each verdict as a packet whose comment carries the medium's reasoning."""
    def record(outcome: TxOutcome) -> None:
        f = outcome.frame
        base = (f"tx_status={outcome.status} attempts={outcome.attempts} "
                f"airtime_us={outcome.airtime_us}")
        if not outcome.receivers:
            # Rejected before transmission, or sent to a MAC nobody holds: record the attempt.
            writer.write_packet(f.start_us + outcome.airtime_us, _packet(f, -100.0),
                                base + f" reason={outcome.reason}")
        for r in outcome.receivers:
            writer.write_packet(r.delivery_us, _packet(f, r.rssi_dbm),
                                base + f" rx_node={r.node_id} delivered={r.delivered} "
                                f"snr_db={r.snr_db:.2f} per={r.per:.3e} reason={r.reason}")
    return record


def _packet(f: Frame, rssi_dbm: float) -> bytes:
    header = radiotap.build_radiotap(f.rate_mbps, f.channel, rssi_dbm)
    from_station = f.src_node != AP_NODE_ID
    if f.mgmt is not None:
        bssid = f.dst_mac if from_station else f.src_mac
        return header + radiotap.build_dot11_mgmt(f.mgmt, f.src_mac, f.dst_mac, bssid, f.payload, f.seq)
    if f.wifi:
        return header + radiotap.build_dot11_data(f.src_mac, f.dst_mac, from_station, f.payload, f.seq)
    return header + radiotap.build_dot11_action(f.src_mac, f.dst_mac, f.payload, f.seq)
