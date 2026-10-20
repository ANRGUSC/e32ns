"""Regression tests for the code-review findings (see RESEARCH_LOG.md D-018).

BUG-1: a frame rejected before transmission must not act as an RF interferer.
BUG-2: a stale SLICE_DONE (wrong slice) must not release the barrier.
BUG-3: a unicast RX lands when the data frame ends, before the ACK tail.
"""

import pytest

from esp_netsim.medium import MediumEngine, MediumConfig, Frame
from esp_netsim.channel.error_model import ConstantErrorModel
from esp_netsim.channel.airtime import AirtimeModel
from esp_netsim.orchestrator.simulation import Simulation
from esp_netsim import protocol
from esp_netsim.protocol import BROADCAST_MAC

MAC1 = "aa:bb:cc:00:00:01"
MAC2 = "aa:bb:cc:00:00:02"
MAC3 = "aa:bb:cc:00:00:03"
OTHER_CHANNEL = "aa:bb:cc:00:00:09"


def _frame(src_node, src_mac, dst, start, ch=1):
    return Frame(src_node, src_mac, dst, ch, 1.0, 1, b"x" * 20, start)


def test_bug1_rejected_frame_does_not_interfere():
    # node1 unicasts to a peer on another channel (rejected, no radiation), then node2
    # broadcasts overlapping in time; node3 must still receive node2's frame.
    eng = MediumEngine(MediumConfig(error_model=ConstantErrorModel(0.0),
                                    capture_threshold_db=6.0))
    eng.add_node(1, MAC1, x=-10, y=0)
    eng.add_node(2, MAC2, x=10, y=0)
    eng.add_node(3, MAC3, x=0, y=0)
    eng.add_node(4, OTHER_CHANNEL, x=50, y=0, channel=6)

    rej = eng.transmit(_frame(1, MAC1, OTHER_CHANNEL, start=0))
    assert rej.status == "fail" and rej.reason == "channel_mismatch"
    assert rej.attempts == 0

    # Same start time: had the rejected frame been registered in-flight, this would
    # collide. It must not.
    out = eng.transmit(_frame(2, MAC2, BROADCAST_MAC, start=0))
    v3 = [r for r in out.receivers if r.node_id == 3][0]
    assert v3.delivered
    assert v3.reason == "ok"


def test_bug3_unicast_rx_precedes_ack_tail():
    eng = MediumEngine(MediumConfig(error_model=ConstantErrorModel(0.0)))
    eng.add_node(1, MAC1, x=0, y=0)
    eng.add_node(2, MAC2, x=1, y=0)
    out = eng.transmit(_frame(1, MAC1, MAC2, start=0))
    assert out.status == "success"
    rx = out.receivers[0]
    ack_tail = AirtimeModel().ack_tail_us()
    # The sender's send-callback fires at full airtime; the RX lands ack_tail earlier
    # (ignoring the ~0 us propagation over 1 m).
    status_t = 0 + out.airtime_us
    assert rx.delivery_us == status_t - ack_tail  # prop ~ 0 at 1 m
    assert rx.delivery_us < status_t


@pytest.mark.asyncio
async def test_bug2_stale_slice_done_ignored():
    eng = MediumEngine(MediumConfig())
    eng.add_node(1, MAC1)
    eng.add_node(2, MAC2)
    sim = Simulation(eng, slice_us=1000)
    # Simulate being mid-slice with until_us=1000, expecting nodes {1,2}.
    sim._expected = {1, 2}
    sim._done = set()
    sim._current_until_us = 1000
    sim._barrier.clear()

    # Stale ack for a previous slice (t=500) must be ignored.
    await sim._on_message(1, protocol.slice_done(1, 500), None)
    assert sim._done == set()
    assert not sim._barrier.is_set()

    # Correct acks for the current slice release the barrier.
    await sim._on_message(1, protocol.slice_done(1, 1000), None)
    await sim._on_message(2, protocol.slice_done(2, 1000), None)
    assert sim._done == {1, 2}
    assert sim._barrier.is_set()
