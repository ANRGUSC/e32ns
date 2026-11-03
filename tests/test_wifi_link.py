"""Association with the virtual AP: probe, auth and assoc frames cross the modeled link."""

import asyncio

import pytest

from esp_netsim.config.schema import parse_scenario
from esp_netsim.orchestrator.simulation import Simulation
from esp_netsim.orchestrator import wifi_link

pytestmark = pytest.mark.asyncio


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
    sim.add_outcome_hook(frames.append)
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

