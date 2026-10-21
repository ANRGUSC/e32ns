"""SliceTime-style barrier clock: per slice, release nodes, collect frames, process, dispatch (D-004)."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .. import protocol
from ..protocol import Message
from ..medium import MediumEngine, Frame, TxOutcome
from ..transport.server import MediumServer, Connection

OutcomeHook = Callable[[TxOutcome], None]
FrameSink = Callable[[], Awaitable[None]]


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
    def __init__(self, engine: MediumEngine, slice_us: int = 1000, port: int = 0):
        self.engine = engine
        self.slice_us = slice_us
        self.server = MediumServer(port)
        self.server.set_handler(self._on_message)
        self.stats = SimStats()
        self.outcome_hooks: list[OutcomeHook] = []
        self.now_us = 0
        # QEMU nodes are paused/resumed over QMP; every other connected node is a mock
        # driven by RUN_SLICE/SLICE_DONE.
        self.qemu_nodes: dict[int, object] = {}

        self._slice_frames: list[Frame] = []
        self._slice_sinks: list[Optional[FrameSink]] = []
        self._due: list[tuple[int, int, FrameSink]] = []   # heap of deliveries by virtual time
        self._tiebreak = itertools.count()
        self._done: set[int] = set()
        self._expected: set[int] = set()
        self._current_until_us: int = -1
        self._barrier = asyncio.Event()

    async def start(self) -> int:
        await self.server.start()
        return self.server.actual_port

    async def stop(self) -> None:
        await self.server.broadcast(protocol.shutdown())
        await self.server.stop()

    def add_outcome_hook(self, hook: OutcomeHook) -> None:
        self.outcome_hooks.append(hook)

    def register_qemu_node(self, node_id: int, qmp) -> None:
        """Gate a QEMU node (anything with async cont()/stop()) on the barrier."""
        self.qemu_nodes[node_id] = qmp

    def offer(self, frame: Frame, sink: FrameSink) -> None:
        """Put a non-ESP-NOW frame on the air this slice; sink runs if and when it is delivered."""
        self._slice_frames.append(frame)
        self._slice_sinks.append(sink)

    async def wait_for_nodes(self, count: int, timeout: float = 5.0) -> None:
        """Block until `count` nodes have connected and said HELLO."""
        async def _poll():
            while len(self.server.connected_nodes) < count:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(_poll(), timeout)

    async def _on_message(self, node_id: Optional[int], msg: Message,
                          conn: Connection) -> None:
        if msg.type == protocol.HELLO:
            await conn.send(protocol.welcome(msg.node, self.slice_us,
                                             self.engine.node(msg.node).mac))
        elif msg.type == protocol.ADD_PEER:
            self.engine.add_peer(msg.node, msg.get("peer"), msg.get("channel", 0),
                                 msg.get("encrypt", False), msg.get("lmk"))
        elif msg.type == protocol.TX:
            self._slice_frames.append(Frame(
                src_node=msg.node, src_mac=msg.get("src"), dst_mac=msg.get("dst"),
                channel=msg.get("channel"), rate_mbps=msg.get("rate_mbps", 1.0),
                seq=msg.get("seq"), payload=msg.get("data"),
                start_us=self._start_time(msg)))
            self._slice_sinks.append(None)
        elif msg.type == protocol.SLICE_DONE:
            # A stale ack from an earlier slice must not release this barrier.
            if msg.get("t") == self._current_until_us:
                self._done.add(msg.node)
                if self._expected and self._done >= self._expected:
                    self._barrier.set()

    def _start_time(self, msg: Message) -> int:
        # Guest esp_timers drift from host time and from each other under stop/cont,
        # so a QEMU frame is placed at the start of the slice it arrived in; mocks are exact.
        return self.now_us if msg.node in self.qemu_nodes else msg.get("t", self.now_us)

    async def run(self, duration_us: int, slice_timeout: float = 5.0) -> SimStats:
        """Advance virtual time in slices until `duration_us` is reached."""
        while self.now_us < duration_us:
            until = min(self.now_us + self.slice_us, duration_us)
            self.now_us += await self._run_one_slice(until, slice_timeout)
            self.stats.slices += 1
            await self._flush_due()
        return self.stats

    async def _run_one_slice(self, until_us: int, slice_timeout: float) -> int:
        """Run every node through one slice and settle its frames; return the virtual time elapsed."""
        self._done = set()
        self._current_until_us = until_us
        self._barrier.clear()
        self._expected = set(self.server.connected_nodes) - set(self.qemu_nodes)

        await asyncio.gather(*(self.server.send(nid, protocol.run_slice(until_us))
                               for nid in self._expected))
        elapsed, _ = await asyncio.gather(self._run_qemu((until_us - self.now_us) / 1e6),
                                          self._wait_for_acks(slice_timeout))

        # Frames can land between slices (slirp delivers asynchronously); they join the next batch.
        frames, sinks = self._slice_frames, self._slice_sinks
        self._slice_frames, self._slice_sinks = [], []
        if frames:
            for outcome, sink in zip(self.engine.process_batch(frames), sinks):
                self._schedule(outcome, sink)
        return max(until_us - self.now_us, elapsed)

    async def _run_qemu(self, wall_s: float) -> int:
        """Run the QEMU nodes for about wall_s; return the guest time that actually passed (us)."""
        if not self.qemu_nodes:
            return 0
        resumed = await asyncio.gather(*(_timed(q.cont()) for q in self.qemu_nodes.values()))
        # asyncio.sleep rounds up to epoll's 1 ms; the guests run through every bit of it.
        time.sleep(wall_s)
        paused = await asyncio.gather(*(_timed(q.stop()) for q in self.qemu_nodes.values()))
        # Without -icount the guest clock follows the host clock while running; QEMU applies
        # cont and stop just before replying, so each node ran from one reply to the other.
        return round(sum(p - r for p, r in zip(paused, resumed)) / len(paused) * 1e6)

    async def _wait_for_acks(self, slice_timeout: float) -> None:
        if not self._expected:
            return
        try:
            await asyncio.wait_for(self._barrier.wait(), slice_timeout)
        except asyncio.TimeoutError:
            pass  # a stalled mock must not hang the run; its frames arrive late or never

    def _at(self, t_us: int, deliver: FrameSink) -> None:
        heapq.heappush(self._due, (t_us, next(self._tiebreak), deliver))

    async def _flush_due(self) -> None:
        while self._due and self._due[0][0] <= self.now_us:
            await heapq.heappop(self._due)[2]()

    def _schedule(self, outcome: TxOutcome, sink: Optional[FrameSink]) -> None:
        """Record a verdict and queue what the nodes will see, each at its computed virtual time."""
        self.stats.record(outcome)
        for hook in self.outcome_hooks:
            hook(outcome)
        f = outcome.frame
        delivered = [r for r in outcome.receivers if r.delivered]
        if sink is not None:
            if delivered:
                self._at(delivered[0].delivery_us, sink)
            return
        self._at(f.start_us + outcome.airtime_us, lambda: self._send_if_connected(
            f.src_node, protocol.tx_status(f.src_node, f.seq, outcome.status,
                                           f.start_us + outcome.airtime_us)))
        for r in delivered:
            self._at(r.delivery_us, lambda r=r: self._send_if_connected(r.node_id, protocol.rx(
                r.node_id, r.delivery_us, f.src_mac, f.dst_mac, f.channel, f.rate_mbps,
                r.rssi_dbm, f.payload)))

    async def _send_if_connected(self, node_id: int, msg: Message) -> None:
        if node_id in self.server.connected_nodes:
            await self.server.send(node_id, msg)


async def _timed(reply: Awaitable) -> float:
    await reply
    return time.monotonic()
