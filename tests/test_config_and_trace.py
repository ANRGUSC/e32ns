"""Tests for config parsing/validation (schema.py) and pcapng tracing (trace/*)."""

import io
import struct

import pytest

from esp_netsim.config.schema import parse_scenario, load_scenario
from esp_netsim.orchestrator.wifi_link import is_medium_control
from esp_netsim.trace.pcapng import PcapngWriter, LINKTYPE_IEEE802_11_RADIOTAP
from esp_netsim.trace import radiotap


# ---- config ---------------------------------------------------------------

def _valid_doc():
    return {
        "name": "t",
        "nodes": [
            {"id": 1, "mac": "aa:bb:cc:00:00:01", "pos": [0, 0], "channel": 1},
            {"id": 2, "mac": "aa:bb:cc:00:00:02", "pos": [5, 0], "channel": 1},
        ],
        "medium": {"error_model": "constant", "packet_error_rate": 0.1},
        "run": {"duration_us": 5000, "slice_us": 1000, "seed": 3},
    }


def test_parse_valid_scenario_builds_engine():
    sc = parse_scenario(_valid_doc())
    assert sc.name == "t" and len(sc.nodes) == 2
    assert sc.slice_us == 1000 and sc.seed == 3
    engine = sc.build_engine()
    assert engine.node(1).mac == "aa:bb:cc:00:00:01"
    assert engine.node(2).x == 5.0


def test_parse_rejects_duplicate_ids():
    doc = _valid_doc()
    doc["nodes"][1]["id"] = 1
    with pytest.raises(ValueError, match="duplicate node id"):
        parse_scenario(doc)


def test_parse_rejects_bad_error_model():
    doc = _valid_doc()
    doc["medium"]["error_model"] = "wormhole"
    with pytest.raises(ValueError, match="error_model"):
        parse_scenario(doc)


def test_parse_rejects_missing_nodes():
    with pytest.raises(ValueError, match="nodes"):
        parse_scenario({"name": "x"})


def test_load_scenario_from_file(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(
        "name: file_scn\n"
        "nodes:\n"
        "  - {id: 1, mac: 'aa:bb:cc:00:00:01', pos: [0,0]}\n"
        "  - {id: 2, mac: 'aa:bb:cc:00:00:02', pos: [1,0]}\n"
        "medium: {error_model: snr_per}\n"
        "run: {duration_us: 1000, slice_us: 500}\n",
        encoding="utf-8",
    )
    sc = load_scenario(str(p))
    assert sc.name == "file_scn" and sc.medium.error_model == "snr_per"


# ---- radiotap -------------------------------------------------------------

def test_channel_to_freq():
    assert radiotap.channel_to_freq_mhz(1) == 2412
    assert radiotap.channel_to_freq_mhz(6) == 2437
    assert radiotap.channel_to_freq_mhz(11) == 2462


def test_radiotap_header_length_and_present():
    hdr = radiotap.build_radiotap(rate_mbps=1.0, channel=6, rssi_dbm=-50)
    # it_version=0, it_pad=0, it_len (u16), it_present (u32)
    ver, pad, it_len, present = struct.unpack_from("<BBHI", hdr, 0)
    assert ver == 0 and it_len == len(hdr) == 15
    assert present == radiotap._RT_PRESENT


def test_dot11_action_has_espressif_oui_and_addrs():
    frame = radiotap.build_dot11_action("aa:bb:cc:00:00:01", "ff:ff:ff:ff:ff:ff",
                                        b"payload", seq=3)
    # Addr1 (dst) at offset 4, Addr2 (src) at offset 10.
    assert frame[4:10] == bytes.fromhex("ffffffffffff")
    assert frame[10:16] == bytes.fromhex("aabbcc000001")
    assert radiotap.ESPRESSIF_OUI in frame
    assert frame.endswith(b"payload")


# ---- pcapng ---------------------------------------------------------------

def _read_blocks(buf: bytes):
    """Minimal pcapng block walker: yields (block_type, total_len, body)."""
    off = 0
    out = []
    while off + 8 <= len(buf):
        btype, total = struct.unpack_from("<II", buf, off)
        body = buf[off + 8: off + total - 4]
        out.append((btype, total, body))
        off += total
    return out


def test_pcapng_structure_and_comment():
    buf = io.BytesIO()
    w = PcapngWriter(buf, interface_name="test-if")
    w.write_packet(1234567, radiotap.build_frame("aa:bb:cc:00:00:01", "ff:ff:ff:ff:ff:ff",
                                                 b"hello", 6, 1.0, -60.0, 1),
                   "reason=ok snr_db=25.00")
    data = buf.getvalue()
    blocks = _read_blocks(data)
    types = [b[0] for b in blocks]
    # SHB, IDB, EPB in order.
    assert types == [0x0A0D0D0A, 0x00000001, 0x00000006]

    # IDB advertises the radiotap link type.
    idb_body = blocks[1][2]
    (linktype,) = struct.unpack_from("<H", idb_body, 0)
    assert linktype == LINKTYPE_IEEE802_11_RADIOTAP

    # EPB timestamp reconstructs to the microsecond value we wrote.
    epb = blocks[2][2]
    _iface, ts_high, ts_low, caplen, origlen = struct.unpack_from("<IIIII", epb, 0)
    assert (ts_high << 32 | ts_low) == 1234567
    # Comment text is present somewhere in the block.
    assert b"reason=ok" in data

    # Every block total length is a multiple of 4 (32-bit alignment).
    assert all(total % 4 == 0 for _t, total, _b in blocks)


def _tcp_frame(src_ip: bytes, dst_ip: bytes, sport: int, dport: int) -> bytes:
    eth = bytes(12) + b"\x08\x00"
    ip = bytes([0x45, 0]) + bytes(7) + bytes([6]) + bytes(2) + src_ip + dst_ip
    return eth + ip + struct.pack(">HH", sport, dport) + bytes(16)


def test_medium_control_link_bypasses_the_wifi_model():
    guest, host, web = bytes((10, 0, 2, 15)), bytes((10, 0, 2, 2)), bytes((93, 184, 215, 14))
    assert is_medium_control(_tcp_frame(guest, host, 40000, 52333), 52333)
    assert is_medium_control(_tcp_frame(host, guest, 52333, 40000), 52333)
    assert not is_medium_control(_tcp_frame(guest, web, 40000, 80), 52333)
    assert not is_medium_control(_tcp_frame(guest, host, 40000, 8080), 52333)


def test_wifi_data_frame_carries_the_ip_packet():
    eth = bytes(range(6)) + bytes(range(6, 12)) + b"\x08\x00" + b"IPDATA"
    frame = radiotap.build_dot11_data("aa:bb:cc:00:00:01", "02:00:00:00:00:01", True, eth)
    assert frame[:2] == b"\x08\x01"                          # data frame, ToDS
    assert frame[24:] == b"\xaa\xaa\x03\x00\x00\x00\x08\x00IPDATA"

