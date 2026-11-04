"""Association with the virtual AP over the modeled link, and the bridge's frame layouts."""

import asyncio
import struct

from esp_netsim.config.schema import parse_scenario
from esp_netsim.orchestrator.simulation import Simulation
from esp_netsim.orchestrator import wifi_link


def _scenario(station_pos, links=()):
    return parse_scenario({
        "name": "assoc",
        "wifi_ap": {"pos": [0, 0], "channel": 6, "ssid": "lab"},
        "nodes": [{"id": 1, "mac": "aa:bb:cc:00:00:01", "pos": station_pos, "channel": 6}],
        "links": list(links),
        "run": {"seed": 1},
    })


async def _associate(sc, ssid):
    sim = Simulation(sc.build_engine(), slice_us=250)
    frames = []
    sim.outcome_hooks.append(frames.append)
    run = asyncio.create_task(sim.run(duration_us=10_000_000))
    result = await wifi_link.associate(sim, wifi_link.station_link(sc.nodes[0], sc.ap), ssid)
    run.cancel()
    return result, [(o.frame.mgmt, o.status) for o in frames]


async def test_near_station_associates_after_three_exchanges():
    (reason, rssi), frames = await _associate(_scenario([25, 0]), "lab")
    assert reason == wifi_link.ASSOCIATED and -70 < rssi < -50
    assert [m for m, _ in frames] == [wifi_link.PROBE_REQ, wifi_link.PROBE_RESP, wifi_link.AUTH,
                                      wifi_link.AUTH, wifi_link.ASSOC_REQ, wifi_link.ASSOC_RESP]
    assert all(s == "success" for _, s in frames)


async def test_management_and_data_frames_share_a_sequence_counter():
    sc = _scenario([25, 0])
    link = wifi_link.station_link(sc.nodes[0], sc.ap)
    sim = Simulation(sc.build_engine(), slice_us=250)
    run = asyncio.create_task(sim.run(duration_us=10_000_000))
    await wifi_link.associate(sim, link, "lab")
    run.cancel()
    assert link.up.frame(0, b"").seq == 4  # probe, auth, assoc request, then data


async def test_out_of_range_station_finds_no_ap():
    (reason, _), frames = await _associate(_scenario([3000, 0]), "lab")
    assert reason == wifi_link.NO_AP_FOUND
    assert frames == [(wifi_link.PROBE_REQ, "fail")]


async def test_wrong_ssid_gets_no_probe_response():
    (reason, _), frames = await _associate(_scenario([25, 0]), "other")
    assert reason == wifi_link.NO_AP_FOUND
    assert frames == [(wifi_link.PROBE_REQ, "success")]


async def test_ap_link_override_applies_to_association():
    sc = _scenario([25, 0], links=[{"a": 1, "b": 0, "rssi_dbm": -120}])
    (reason, _), _ = await _associate(sc, "lab")
    assert reason == wifi_link.NO_AP_FOUND



def _tcp_frame(src_ip: bytes, dst_ip: bytes, sport: int, dport: int) -> bytes:
    ip = bytes((0x45, 0)) + bytes(7) + bytes((6,)) + bytes(2) + src_ip + dst_ip
    return bytes(12) + bytes.fromhex("0800") + ip + struct.pack(">HH", sport, dport) + bytes(16)


def test_medium_control_link_bypasses_the_wifi_model():
    guest, host, web = bytes((10, 0, 2, 15)), bytes((10, 0, 2, 2)), bytes((93, 184, 215, 14))
    assert wifi_link.is_medium_control(_tcp_frame(guest, host, 40000, 52333), 52333)
    assert wifi_link.is_medium_control(_tcp_frame(host, guest, 52333, 40000), 52333)
    assert not wifi_link.is_medium_control(_tcp_frame(guest, web, 40000, 80), 52333)
    assert not wifi_link.is_medium_control(_tcp_frame(guest, host, 40000, 8080), 52333)


def test_connect_request_and_reply_layout():
    header = bytes(6) + bytes.fromhex("aabbcc000001") + wifi_link.ETHERTYPE_CTRL
    assert wifi_link.parse_connect_request(header + bytes((wifi_link.OP_CONNECT, 3)) + b"lab") == "lab"
    reply = wifi_link.connect_reply("aa:bb:cc:00:00:01", "02:00:00:00:00:01",
                                    wifi_link.ASSOCIATED, 6, -61.6)
    assert reply[12:] == wifi_link.ETHERTYPE_CTRL + bytes(
        (wifi_link.OP_CONNECT_REPLY, wifi_link.ASSOCIATED, 6)) + (-62).to_bytes(1, "big", signed=True)
