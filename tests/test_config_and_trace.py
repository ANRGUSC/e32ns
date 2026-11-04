"""Scenario validation at the config boundary, and the pcapng/802.11 trace bytes."""

import io
import struct

import pytest

from esp_netsim.config.schema import parse_scenario
from esp_netsim.trace import radiotap
from esp_netsim.trace.pcapng import LINKTYPE_IEEE802_11_RADIOTAP, PcapngWriter


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
    engine = parse_scenario(_valid_doc()).build_engine()
    assert engine.node(2).x == 5.0
    assert engine.cfg.seed == 3 and engine.cfg.error_model.packet_error_rate == 0.1


def test_parse_links_override_the_medium():
    doc = {**_valid_doc(), "links": [{"a": 2, "b": 1, "rssi_dbm": -80, "loss": 0.25}]}
    rssi, per = parse_scenario(doc).build_engine().link_quality(1, 2, 50, 1.0)
    assert rssi == -80.0 and per >= 0.25


def _with(path, value):
    doc = _valid_doc()
    *parents, key = path
    target = doc
    for p in parents:
        target = target[p]
    target[key] = value
    return doc


@pytest.mark.parametrize("doc, msg", [
    (_with(["nodes", 1, "id"], 1), "duplicate node id"),
    (_with(["nodes", 1, "mac"], "aa:bb:cc:00:00:0g"), "bad MAC"),
    (_with(["nodes", 0, "pos"], [0, 0, 3]), "pos"),
    (_with(["medium", "error_model"], "wormhole"), "error_model"),
    (_with(["medium", "capture_threshold_db"], 4.0), "unknown key 'capture_threshold_db'"),
    (_with(["run", "slice_us"], 0), "slice_us"),
    (_with(["traffic"], [{"node": 1, "at_us": 0, "count": 0}]), "count"),
    (_with(["links"], [{"a": 1, "b": 9, "loss": 0.1}]), "node 9"),
    (_with(["links"], [{"a": 1, "b": 1, "loss": 0.1}]), "itself"),
    (_with(["links"], [{"a": 1, "b": 2, "loss": 1.5}]), "loss"),
    (_with(["links"], [{"a": 1, "b": 2}]), "rssi_dbm or loss"),
    ({"name": "x"}, "nodes"),
])
def test_parse_rejects(doc, msg):
    with pytest.raises(ValueError, match=msg):
        parse_scenario(doc)


def test_radiotap_header():
    hdr = radiotap.build_radiotap(rate_mbps=1.0, channel=6, rssi_dbm=-50)
    assert struct.unpack_from("<BBHI", hdr) == (0, 0, 15, 0x2E)   # flags, rate, channel, signal
    assert struct.unpack_from("<BBHHb", hdr, 8) == (0, 2, 2437, 0x00A0, -50)


def test_dot11_action_has_espressif_oui_and_addrs():
    frame = radiotap.build_dot11_action("aa:bb:cc:00:00:01", "ff:ff:ff:ff:ff:ff", b"payload", 3)
    assert frame[4:10] == bytes.fromhex("ffffffffffff")       # receiver
    assert frame[10:16] == bytes.fromhex("aabbcc000001")      # transmitter
    assert frame.endswith(radiotap.ESPRESSIF_OUI + b"payload")


def test_mgmt_frame_header_carries_subtype_and_bssid():
    body = bytes.fromhex("000001000000")                     # open system, seq 1, success
    frame = radiotap.build_dot11_mgmt(11, "aa:bb:cc:00:00:01", "02:00:00:00:00:01",
                                      "02:00:00:00:00:01", body, 0)
    assert frame[:2] == bytes.fromhex("b000")                 # management, authentication
    assert frame[16:22] == bytes.fromhex("020000000001")      # address 3 is the BSSID
    assert frame[24:] == body


def test_wifi_data_frame_carries_the_ip_packet():
    eth = bytes(range(12)) + bytes.fromhex("0800") + b"IPDATA"
    frame = radiotap.build_dot11_data("aa:bb:cc:00:00:01", "02:00:00:00:00:01", True, eth, 0)
    assert frame[:2] == bytes.fromhex("0801")                 # data frame, ToDS
    assert frame[24:] == bytes.fromhex("aaaa030000000800") + b"IPDATA"


def _blocks(buf: bytes) -> list:
    out, off = [], 0
    while off < len(buf):
        btype, total = struct.unpack_from("<II", buf, off)
        out.append((btype, total, buf[off + 8:off + total - 4]))
        off += total
    return out


def test_pcapng_structure_and_comment():
    buf = io.BytesIO()
    PcapngWriter(buf, "test-if").write_packet(1234567, b"frame", "reason=ok snr_db=25.00")
    blocks = _blocks(buf.getvalue())
    assert [b[0] for b in blocks] == [0x0A0D0D0A, 0x00000001, 0x00000006]   # SHB, IDB, EPB
    assert all(total % 4 == 0 for _, total, _ in blocks)
    assert struct.unpack_from("<H", blocks[1][2])[0] == LINKTYPE_IEEE802_11_RADIOTAP
    _, ts_high, ts_low = struct.unpack_from("<III", blocks[2][2])
    assert (ts_high << 32 | ts_low) == 1234567
    assert b"reason=ok" in blocks[2][2]
