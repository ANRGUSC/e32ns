"""Barrier ordering with QEMU nodes (paused/resumed over QMP) alongside message-gated mocks."""

import asyncio

import pytest

from esp_netsim.medium import MediumEngine, MediumConfig
from esp_netsim.channel.error_model import ConstantErrorModel
from esp_netsim.orchestrator.simulation import Simulation
from esp_netsim.nodes.mock_node import MockNode
from esp_netsim import protocol
from esp_netsim.protocol import BROADCAST_MAC

pytestmark = pytest.mark.asyncio

MAC1 = "aa:bb:cc:00:00:01"
MAC2 = "aa:bb:cc:00:00:02"


class FakeQmp:
    """Records cont/stop; runs an async hook on cont (the guest 'running')."""

    def __init__(self, on_cont):
        self.events = []
        self.on_cont = on_cont

    async def cont(self):
        self.events.append("cont")
        await self.on_cont()

    async def stop(self):
        self.events.append("stop")


async def test_mixed_barrier_qemu_sender_to_mock_receiver():
    engine = MediumEngine(MediumConfig(error_model=ConstantErrorModel(0.0)))
    engine.add_node(1, MAC1, x=0, y=0)   # QEMU-stand-in sender
    engine.add_node(2, MAC2, x=1, y=0)   # message-gated receiver

    sim = Simulation(engine, slice_us=1000)
    port = await sim.start()

    # QEMU stand-in: gated externally (barrier_ack=False), emits when its gate 'cont's.
    qnode = MockNode(1, MAC1, port=port, barrier_ack=False)
    receiver = MockNode(2, MAC2, port=port)
    qnode.schedule_tx(t_us=0, dst_mac=BROADCAST_MAC, payload=b"ping", seq=1)

    fq = FakeQmp(on_cont=lambda: qnode.emit_due(10_000))
    sim.register_qemu_node(1, fq)

    for n in (qnode, receiver):
        await n.connect()
    await sim.wait_for_nodes(2)

    await sim.run(duration_us=3000)   # 3 slices
    await asyncio.sleep(0.1)

    # Every slice resumes then pauses the QEMU node, never overlapping two slices.
    assert fq.events == ["cont", "stop"] * 3
    # ...and the QEMU-stand-in's frame reached the message-gated receiver.
    assert any(r.data == b"ping" for r in receiver.received)
    assert sim.stats.tx_success == 1

    await sim.stop()
    for n in (qnode, receiver):
        await n.close()


async def test_mock_only_run():
    engine = MediumEngine(MediumConfig(error_model=ConstantErrorModel(0.0)))
    engine.add_node(1, MAC1, x=0)
    engine.add_node(2, MAC2, x=1)
    sim = Simulation(engine, slice_us=1000)
    port = await sim.start()
    a = MockNode(1, MAC1, port=port)
    b = MockNode(2, MAC2, port=port)
    a.schedule_tx(t_us=100, dst_mac=MAC2, payload=b"x", seq=1)
    for n in (a, b):
        await n.connect()
    await sim.wait_for_nodes(2)
    await sim.run(duration_us=2000)
    await asyncio.sleep(0.1)
    assert a.send_status.get(1) == "success"
    assert any(r.data == b"x" for r in b.received)
    await sim.stop()
    for n in (a, b):
        await n.close()


async def test_rx_is_held_until_its_computed_delivery_time():
    engine = MediumEngine(MediumConfig(error_model=ConstantErrorModel(0.0)))
    engine.add_node(1, MAC1, x=0)
    engine.add_node(2, MAC2, x=1)
    sim = Simulation(engine, slice_us=250)
    port = await sim.start()
    a = MockNode(1, MAC1, port=port)
    b = MockNode(2, MAC2, port=port)
    a.schedule_tx(t_us=0, dst_mac=BROADCAST_MAC, payload=b"x" * 16, seq=1)
    for n in (a, b):
        await n.connect()
    await sim.wait_for_nodes(2)
    await sim.run(duration_us=250)   # the frame's airtime (~0.5 ms) outlasts one slice
    await asyncio.sleep(0.05)
    assert b.received == []
    await sim.run(duration_us=1500)
    await asyncio.sleep(0.05)
    assert [r.data for r in b.received] == [b"x" * 16]
    assert b.received[0].t_us > 250
    await sim.stop()
    for n in (a, b):
        await n.close()


async def test_stale_slice_done_does_not_release_the_barrier():
    eng = MediumEngine(MediumConfig())
    eng.add_node(1, MAC1)
    eng.add_node(2, MAC2)
    sim = Simulation(eng, slice_us=1000)
    sim._expected, sim._done, sim._current_until_us = {1, 2}, set(), 1000
    await sim._on_message(1, protocol.slice_done(1, 500), None)
    assert not sim._barrier.is_set()
    await sim._on_message(1, protocol.slice_done(1, 1000), None)
    await sim._on_message(2, protocol.slice_done(2, 1000), None)
    assert sim._barrier.is_set()
