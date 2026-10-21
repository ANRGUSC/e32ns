"""End-to-end tests over real TCP loopback: medium + server + barrier + mock nodes.

These exercise the full vertical slice with no QEMU: protocol framing,
transport, the SliceTime barrier, and ESP-NOW send/recv semantics.
"""

import asyncio
import json

import pytest

from esp_netsim.medium import MediumEngine, MediumConfig
from esp_netsim.channel.error_model import ConstantErrorModel
from esp_netsim.orchestrator.simulation import Simulation
from esp_netsim.nodes.mock_node import MockNode
from esp_netsim.protocol import BROADCAST_MAC
from esp_netsim.config.schema import parse_scenario
from esp_netsim.orchestrator.runner import run_scenario

pytestmark = pytest.mark.asyncio

MAC1 = "aa:bb:cc:00:00:01"
MAC2 = "aa:bb:cc:00:00:02"
MAC3 = "aa:bb:cc:00:00:03"


async def _drain(seconds: float = 0.1):
    await asyncio.sleep(seconds)


async def test_two_node_broadcast_end_to_end():
    engine = MediumEngine(MediumConfig(error_model=ConstantErrorModel(0.0)))
    engine.add_node(1, MAC1, x=0, y=0)
    engine.add_node(2, MAC2, x=5, y=0)
    engine.add_node(3, MAC3, x=10, y=0)

    sim = Simulation(engine, slice_us=1000)
    port = await sim.start()

    n1 = MockNode(1, MAC1, port=port)
    n2 = MockNode(2, MAC2, port=port)
    n3 = MockNode(3, MAC3, port=port)
    n1.schedule_tx(t_us=500, dst_mac=BROADCAST_MAC, payload=b"beacon", seq=1)
    for n in (n1, n2, n3):
        await n.connect()
    await sim.wait_for_nodes(3)

    await sim.run(duration_us=3000)
    await _drain()

    # Sender got a success callback (broadcast is unacknowledged-success).
    assert n1.send_status.get(1) == "success"
    # Both other nodes received the broadcast.
    assert any(r.data == b"beacon" for r in n2.received)
    assert any(r.data == b"beacon" for r in n3.received)
    assert sim.stats.tx_success == 1

    await sim.stop()
    for n in (n1, n2, n3):
        await n.close()


async def test_unicast_controlled_loss_end_to_end():
    # Force total loss -> unicast send callback must be "fail" and no RX arrives.
    engine = MediumEngine(MediumConfig(error_model=ConstantErrorModel(1.0), max_retries=2))
    engine.add_node(1, MAC1, x=0, y=0)
    engine.add_node(2, MAC2, x=5, y=0)

    sim = Simulation(engine, slice_us=1000)
    port = await sim.start()

    n1 = MockNode(1, MAC1, port=port)
    n2 = MockNode(2, MAC2, port=port)
    n1.schedule_tx(t_us=200, dst_mac=MAC2, payload=b"ping", seq=7)
    for n in (n1, n2):
        await n.connect()
    await sim.wait_for_nodes(2)

    await sim.run(duration_us=5000)   # 3 lost attempts: the send callback lands ~2.9 ms in
    await _drain()

    assert n1.send_status.get(7) == "fail"
    assert n2.received == []
    assert sim.stats.tx_fail == 1

    await sim.stop()
    for n in (n1, n2):
        await n.close()


async def test_unicast_success_end_to_end():
    engine = MediumEngine(MediumConfig(error_model=ConstantErrorModel(0.0)))
    engine.add_node(1, MAC1, x=0, y=0)
    engine.add_node(2, MAC2, x=3, y=0)

    sim = Simulation(engine, slice_us=1000)
    port = await sim.start()

    n1 = MockNode(1, MAC1, port=port)
    n2 = MockNode(2, MAC2, port=port)
    n1.schedule_tx(t_us=100, dst_mac=MAC2, payload=b"ping", seq=1)
    for n in (n1, n2):
        await n.connect()
    await sim.wait_for_nodes(2)
    await sim.run(duration_us=2000)
    await _drain()

    assert n1.send_status.get(1) == "success"
    assert any(r.data == b"ping" for r in n2.received)
    # RX carried an RSSI value (radiotap-style metadata).
    got = [r for r in n2.received if r.data == b"ping"][0]
    assert got.rssi_dbm < 0  # dBm receive power

    await sim.stop()
    for n in (n1, n2):
        await n.close()


async def test_run_scenario_writes_results(tmp_path):
    doc = {"name": "t",
           "nodes": [{"id": 1, "mac": "aa:bb:cc:00:00:01", "pos": [0, 0]},
                     {"id": 2, "mac": "aa:bb:cc:00:00:02", "pos": [5, 0]}],
           "medium": {"error_model": "constant", "packet_error_rate": 0.0},
           "run": {"duration_us": 20000, "out_dir": str(tmp_path)},
           "traffic": [{"node": 1, "dst": "broadcast", "at_us": 1000, "count": 3,
                        "period_us": 5000}]}
    result = await run_scenario(parse_scenario(doc))
    saved = json.loads((tmp_path / "results.json").read_text())
    assert result.stats.rx_delivered == 3
    assert [(e["src"], e["rx"], e["delivered"]) for e in saved["events"]] == [(1, 2, True)] * 3
    assert (tmp_path / "trace.pcapng").stat().st_size > 0
    assert '"name": "t"' in (tmp_path / "report.html").read_text(encoding="utf-8")
