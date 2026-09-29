"""Run a ScenarioConfig in-process with mock nodes: server + barrier + traffic + trace."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from ..config.schema import ScenarioConfig
from ..nodes.mock_node import MockNode
from ..trace.pcapng import PcapngWriter, TraceRecorder
from .simulation import Simulation, SimStats


@dataclass
class RunResult:
    stats: SimStats
    trace_path: Optional[str]
    nodes: dict            # node_id -> MockNode (for inspection in tests)


async def run_scenario(scenario: ScenarioConfig,
                       trace_override: Optional[str] = None) -> RunResult:
    engine = scenario.build_engine()
    sim = Simulation(engine, slice_us=scenario.slice_us, port=scenario.port)

    trace_path = trace_override or scenario.trace_path
    trace_fp = None
    if trace_path:
        os.makedirs(os.path.dirname(os.path.abspath(trace_path)), exist_ok=True)
        trace_fp = open(trace_path, "wb")
        recorder = TraceRecorder(PcapngWriter(trace_fp, interface_name=scenario.name))
        sim.add_outcome_hook(recorder.on_outcome)

    port = await sim.start()

    # Build one mock node per declared node and apply scripted traffic.
    nodes: dict[int, MockNode] = {}
    for nc in scenario.nodes:
        mn = MockNode(nc.node_id, nc.mac, port=port, channel=nc.channel)
        nodes[nc.node_id] = mn

    seq_counter: dict[int, int] = {nid: 0 for nid in nodes}
    for tc in scenario.traffic:
        mn = nodes[tc.node_id]
        for k in range(max(1, tc.count)):
            seq_counter[tc.node_id] += 1
            payload = bytes((seq_counter[tc.node_id] & 0xFF,)) * tc.size_bytes
            mn.schedule_tx(t_us=tc.at_us + k * tc.period_us, dst_mac=tc.dst,
                           payload=payload, rate_mbps=tc.rate_mbps,
                           seq=seq_counter[tc.node_id])

    try:
        for mn in nodes.values():
            await mn.connect()
        await sim.wait_for_nodes(len(nodes))
        stats = await sim.run(duration_us=scenario.duration_us)
        # Let final TX_STATUS/RX messages drain to the mock nodes before teardown.
        await asyncio.sleep(0.1)
    finally:
        await sim.stop()
        for mn in nodes.values():
            await mn.close()
        if trace_fp is not None:
            trace_fp.close()

    return RunResult(stats=stats, trace_path=trace_path, nodes=nodes)


def run_scenario_sync(scenario: ScenarioConfig,
                      trace_override: Optional[str] = None) -> RunResult:
    return asyncio.run(run_scenario(scenario, trace_override))
