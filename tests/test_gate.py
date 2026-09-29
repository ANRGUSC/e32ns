"""Tests for the barrier gate abstraction (orchestrator/gate.py, D-026).

Covers the QmpGate lifecycle and a mixed barrier: a QEMU-stand-in node (gated by a
fake QMP client) sending to a message-gated MockNode receiver, all on one clock master.
"""

import asyncio

import pytest

from esp_netsim.medium import MediumEngine, MediumConfig
from esp_netsim.channel.error_model import ConstantErrorModel
from esp_netsim.orchestrator.simulation import Simulation
from esp_netsim.orchestrator.gate import QmpGate
from esp_netsim.nodes.mock_node import MockNode
from esp_netsim.protocol import BROADCAST_MAC

pytestmark = pytest.mark.asyncio

MAC1 = "aa:bb:cc:00:00:01"
MAC2 = "aa:bb:cc:00:00:02"


class FakeQmp:
    """Records cont/stop; optionally runs an async hook on cont (the guest 'running')."""

    def __init__(self):
        self.conts = 0
        self.stops = 0
        self.on_cont = None

    async def cont(self):
        self.conts += 1
        if self.on_cont is not None:
            await self.on_cont()

    async def stop(self):
        self.stops += 1


async def test_qmpgate_cont_then_stop_order():
    fq = FakeQmp()
    g = QmpGate(node_id=1, qmp=fq, slice_us=200)
    await g.release(until_us=200)
    assert (fq.conts, fq.stops) == (1, 0)   # released = cont
    await g.wait_barrier()
    assert (fq.conts, fq.stops) == (1, 1)   # barrier = (dwell then) stop


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

    fq = FakeQmp()
    fq.on_cont = lambda: qnode.emit_due(10_000)   # guest "runs" during cont -> emits
    sim.register_qmp_gate(QmpGate(node_id=1, qmp=fq, slice_us=5000))  # 5 ms wall/slice

    for n in (qnode, receiver):
        await n.connect()
    await sim.wait_for_nodes(2)

    await sim.run(duration_us=3000)   # 3 slices
    await asyncio.sleep(0.1)

    # The QMP gate was actually driven each slice...
    assert fq.conts >= 1 and fq.stops >= 1
    # ...and the QEMU-stand-in's frame reached the message-gated receiver.
    assert any(r.data == b"ping" for r in receiver.received)
    assert sim.stats.tx_success == 1

    await sim.stop()
    for n in (qnode, receiver):
        await n.close()


async def test_no_qmp_gate_is_unchanged_behavior():
    # With no QMP gate, a plain two-mock-node run still works (regression guard).
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
