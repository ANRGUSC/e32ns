"""SliceTime-style barrier clock: per slice, release nodes, collect frames, process, dispatch (D-004)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional

from .. import protocol
from ..protocol import Message
from ..medium import MediumEngine, Frame, TxOutcome
from ..transport.server import MediumServer, Connection

OutcomeHook = Callable[[TxOutcome], None]


@dataclass
class SimStats:
    slices: int = 0
    tx_total: int = 0
    tx_success: int = 0
    tx_fail: int = 0
    rx_delivered: int = 0
    rx_dropped: int = 0

    def record(self, outcome: TxOutcome) -> None:
        self.tx_total += 1
        if outcome.status == protocol.STATUS_SUCCESS:
            self.tx_success += 1
        else:
            self.tx_fail += 1
        for r in outcome.receivers:
            if r.delivered:
                self.rx_delivered += 1
            else:
                self.rx_dropped += 1


class Simulation:
    def __init__(self, engine: MediumEngine, slice_us: int = 1000,
                 host: str = "127.0.0.1", port: int = 0):
        self.engine = engine
        self.slice_us = slice_us
        self.server = MediumServer(host, port)
        self.server.set_handler(self._on_message)
        self.stats = SimStats()
        self.outcome_hooks: list[OutcomeHook] = []
        self.now_us = 0
        # QMP-gated (real QEMU) nodes, keyed by node id. Message-gated (mock) nodes
        # are all other connected nodes and are driven via RUN_SLICE/SLICE_DONE (D-026).
        self.qmp_gates: dict[int, "object"] = {}

        # Per-slice mutable state (reset each slice).
        self._slice_frames: list[Frame] = []
        self._done: set[int] = set()
        self._expected: set[int] = set()
        self._current_until_us: int = -1   # the until_t of the slice in progress
        self._barrier: asyncio.Event = asyncio.Event()
        self._nodes_ready: asyncio.Event = asyncio.Event()

    # ---- lifecycle ----------------------------------------------------------
    async def start(self) -> int:
        await self.server.start()
        return self.server.actual_port

    async def stop(self) -> None:
        await self.server.broadcast(protocol.shutdown())
        await self.server.stop()

    def add_outcome_hook(self, hook: OutcomeHook) -> None:
        self.outcome_hooks.append(hook)

    def register_qmp_gate(self, gate) -> None:
        """Register a QMP-gated (real QEMU) node as a barrier participant (D-026)."""
        self.qmp_gates[gate.node_id] = gate

    async def wait_for_nodes(self, count: int, timeout: float = 5.0) -> None:
        """Block until `count` nodes have connected+HELLO'd (barrier participants)."""
        async def _poll():
            while len(self.server.connected_nodes) < count:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(_poll(), timeout)

    # ---- message handling ---------------------------------------------------
    async def _on_message(self, node_id: Optional[int], msg: Message,
                          conn: Connection) -> None:
        if msg.type == protocol.HELLO:
            # Node registers; medium already knows it from config/topology. ACK it.
            await conn.send(protocol.welcome(msg.node, self.slice_us,
                                             self.engine.node(msg.node).mac))
        elif msg.type == protocol.ADD_PEER:
            self.engine.add_peer(msg.node, msg.get("peer"), msg.get("channel", 0),
                                 msg.get("encrypt", False),
                                 protocol.hex_to_data(msg.get("lmk")) if msg.get("lmk")
                                 else None)
        elif msg.type == protocol.TX:
            self._slice_frames.append(Frame(
                src_node=msg.node, src_mac=msg.get("src"), dst_mac=msg.get("dst"),
                channel=msg.get("channel"), rate_mbps=msg.get("rate_mbps", 1.0),
                seq=msg.get("seq"), payload=protocol.hex_to_data(msg.get("data")),
                start_us=msg.get("t", self.now_us)))
        elif msg.type == protocol.SLICE_DONE:
            # Only count a SLICE_DONE for the slice currently in progress. A stale ack
            # from a previous slice (possible once real QEMU nodes add latency) must not
            # prematurely release the barrier (review BUG-2).
            if msg.get("t") == self._current_until_us:
                self._done.add(msg.node)
                if self._expected and self._done >= self._expected:
                    self._barrier.set()

    # ---- the barrier clock loop --------------------------------------------
    async def run(self, duration_us: int, slice_timeout: float = 5.0) -> SimStats:
        """Advance virtual time in slices until `duration_us` is reached."""
        end = duration_us
        while self.now_us < end:
            until = min(self.now_us + self.slice_us, end)
            await self._run_one_slice(until, slice_timeout)
            self.now_us = until
            self.stats.slices += 1
        return self.stats

    async def _run_one_slice(self, until_us: int, slice_timeout: float) -> None:
        self._slice_frames = []
        self._done = set()
        self._current_until_us = until_us
        self._barrier.clear()

        # Message-gated participants = connected nodes minus QMP-gated (QEMU) ones.
        qmp_ids = set(self.qmp_gates)
        self._expected = set(self.server.connected_nodes) - qmp_ids

        if not self._expected and not self.qmp_gates:
            return  # nothing connected yet

        # Release everyone concurrently: RUN_SLICE to mock nodes, QMP cont to QEMU nodes.
        releases = [gate.release(until_us) for gate in self.qmp_gates.values()]
        for nid in self._expected:
            releases.append(self.server.send(nid, protocol.run_slice(until_us)))
        if releases:
            await asyncio.gather(*releases)

        # Wait for all participants to reach the barrier: mock nodes ack SLICE_DONE;
        # QEMU nodes are run for the slice's wall-time then stopped (D-004/D-026).
        async def _wait_msg():
            if not self._expected:
                return
            try:
                await asyncio.wait_for(self._barrier.wait(), slice_timeout)
            except asyncio.TimeoutError:
                pass  # a node stalled; proceed (QEMU path force-stops via its gate)

        await asyncio.gather(_wait_msg(),
                             *[gate.wait_barrier() for gate in self.qmp_gates.values()])

        # All participants are paused: process the slice's frames as one batch.
        if self._slice_frames:
            outcomes = self.engine.process_batch(self._slice_frames)
            for outcome in outcomes:
                await self._dispatch_outcome(outcome)

    async def _dispatch_outcome(self, outcome: TxOutcome) -> None:
        self.stats.record(outcome)
        for hook in self.outcome_hooks:
            hook(outcome)

        f = outcome.frame
        # Send-callback verdict to the transmitter (brief §3 — the key behavior).
        status_t = f.start_us + outcome.airtime_us
        if f.src_node in self.server.connected_nodes:
            await self.server.send(f.src_node,
                                   protocol.tx_status(f.src_node, f.seq,
                                                      outcome.status, status_t))
        # RX to each receiver that actually got the frame.
        for r in outcome.receivers:
            if not r.delivered:
                continue
            if r.node_id in self.server.connected_nodes:
                await self.server.send(r.node_id, protocol.rx(
                    r.node_id, r.delivery_us, f.src_mac, f.dst_mac, f.channel,
                    f.rate_mbps, r.rssi_dbm, f.payload))
