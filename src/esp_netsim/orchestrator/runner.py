"""Run a scenario end to end: medium, mock and QEMU nodes on one barrier, results in out_dir."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config.schema import ScenarioConfig
from ..medium import TxOutcome
from ..nodes.mock_node import MockNode
from ..trace.pcapng import PcapngWriter, TraceRecorder
from . import qemu_launcher, wifi_link
from .simulation import Simulation, SimStats


@dataclass
class RunResult:
    stats: SimStats
    out_dir: Path
    events: list   # one dict per (frame, receiver) verdict, as written to results.json


def outcome_rows(outcome: TxOutcome) -> list[dict]:
    """Flatten a medium verdict into one row per receiver (one row if nobody could hear it)."""
    f = outcome.frame
    base = {"t_us": f.start_us, "src": f.src_node, "dst": f.dst_mac, "seq": f.seq,
            "wifi": f.wifi, "bytes": len(f.payload), "status": outcome.status,
            "attempts": outcome.attempts}
    if not outcome.receivers:
        return [{**base, "rx": None, "delivered": False, "reason": outcome.reason}]
    return [{**base, "rx": r.node_id, "delivered": r.delivered, "rx_t_us": r.delivery_us,
             "rssi_dbm": round(r.rssi_dbm, 1), "reason": r.reason}
            for r in outcome.receivers]


def _mock_nodes(scenario: ScenarioConfig, port: int) -> dict[int, MockNode]:
    nodes = {n.node_id: MockNode(n.node_id, n.mac, port=port, channel=n.channel)
             for n in scenario.nodes if n.firmware is None}
    seq = dict.fromkeys(nodes, 0)
    for tc in scenario.traffic:
        for k in range(max(1, tc.count)):
            seq[tc.node_id] += 1
            nodes[tc.node_id].schedule_tx(
                t_us=tc.at_us + k * tc.period_us, dst_mac=tc.dst,
                payload=bytes((seq[tc.node_id] & 0xFF,)) * tc.size_bytes,
                rate_mbps=tc.rate_mbps, seq=seq[tc.node_id])
    return nodes


async def _run_nodes(scenario: ScenarioConfig, sim: Simulation, out: Path) -> SimStats:
    port = await sim.start()
    mocks = _mock_nodes(scenario, port)
    qemu: list[qemu_launcher.QemuNode] = []
    pumps: list[asyncio.Task] = []
    try:
        with tempfile.TemporaryDirectory(prefix="esp-netsim-") as workdir:
            for n in scenario.nodes:
                if n.firmware:
                    q = await qemu_launcher.launch(n, port, Path(workdir))
                    qemu.append(q)
                    sim.register_qemu_node(q.node_id, q.qmp)
                    if n.wifi:
                        pumps += await wifi_link.bridge(sim, n, scenario.ap, q.wifi_sockets)
            for m in mocks.values():
                await m.connect()
            await sim.wait_for_nodes(len(mocks))
            try:
                stats = await sim.run(duration_us=scenario.duration_us)
                await asyncio.sleep(0.1)  # let the last TX_STATUS/RX reach the mocks
            finally:
                for p in pumps:
                    p.cancel()
                await qemu_launcher.terminate(qemu)
                for q in qemu:
                    shutil.copy(q.uart_log, out / f"node{q.node_id}.log")
            return stats
    finally:
        await sim.stop()
        for m in mocks.values():
            await m.close()


def write_results(scenario: ScenarioConfig, result: RunResult) -> None:
    """Write results.json: topology, aggregate stats, and every per-receiver verdict."""
    nodes = [{"id": n.node_id, "mac": n.mac, "pos": [n.x, n.y], "channel": n.channel,
              "firmware": Path(n.firmware).parent.name if n.firmware else "mock"}
             for n in scenario.nodes]
    if scenario.ap:
        ap = scenario.ap
        nodes.append({"id": ap.node_id, "mac": ap.mac, "pos": [ap.x, ap.y],
                      "channel": ap.channel, "firmware": "access point"})
    doc = {"name": scenario.name, "duration_us": scenario.duration_us,
           "slice_us": scenario.slice_us, "nodes": nodes,
           "stats": asdict(result.stats), "events": result.events}
    (result.out_dir / "results.json").write_text(json.dumps(doc, indent=1))


async def run_scenario(scenario: ScenarioConfig) -> RunResult:
    out = Path(scenario.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sim = Simulation(scenario.build_engine(), slice_us=scenario.slice_us, port=scenario.port)
    events: list[dict] = []
    sim.add_outcome_hook(lambda o: events.extend(outcome_rows(o)))
    with open(out / "trace.pcapng", "wb") as trace_fp:
        sim.add_outcome_hook(TraceRecorder(PcapngWriter(trace_fp, scenario.name)).on_outcome)
        stats = await _run_nodes(scenario, sim, out)
    result = RunResult(stats, out, events)
    write_results(scenario, result)
    return result
