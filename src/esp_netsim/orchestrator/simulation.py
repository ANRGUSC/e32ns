"""SliceTime-style barrier clock: per slice, release nodes, collect frames, process, dispatch (D-004)."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .. import protocol
from ..channel import ESPNOW_RATE_MBPS
from ..medium import Frame, MediumEngine, TxOutcome
from ..protocol import Message
from ..transport.server import Connection, MediumServer

OutcomeHook = Callable[[TxOutcome], None]
FrameSink = Callable[[TxOutcome], Awaitable[None]]
TIMEOUT_S = 5.0     # a node that never connects or acks must not hang the run


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
        delivered = sum(r.delivered for r in outcome.receivers)
        self.rx_delivered += delivered
        self.rx_dropped += len(outcome.receivers) - delivered


class Simulation:
    def __init__(self, engine: MediumEngine, slice_us: int):
        self.engine = engine
        self.slice_us = slice_us
        self.server = MediumServer(self._on_message)
        self.stats = SimStats()
        self.outcome_hooks: list[OutcomeHook] = []
        self.now_us = 0
        # QEMU nodes are gated by QMP cont/stop; every other connected node is a mock gated by
        # RUN_SLICE/SLICE_DONE.
        self.qemu_nodes: dict[int, Any] = {}

        self._pending: list[tuple[Frame, Optional[FrameSink]]] = []   # this slice's frames
        self._due: list[tuple[int, int, Callable[[], Awaitable[None]]]] = []  # heap by virtual time
        self._tiebreak = itertools.count()
        self._done: set[int] = set()
        self._expected: set[int] = set()
        self._current_until_us = -1
        self._barrier = asyncio.Event()

    async def start(self) -> int:
        await self.server.start()
        return self.server.port

    async def stop(self) -> None:
        await self.server.broadcast(Message(protocol.SHUTDOWN, {}))
        await self.server.stop()

    def offer(self, frame: Frame, sink: FrameSink) -> None:
        """Put a non-ESP-NOW frame on the air this slice; sink gets its verdict when it is due."""
        self._pending.append((frame, sink))

    async def exchange(self, frame: Frame) -> TxOutcome:
        """Offer a frame and wait, in virtual time, for its verdict."""
        verdict = asyncio.get_running_loop().create_future()

        async def resolve(outcome: TxOutcome) -> None:
            verdict.set_result(outcome)
        self.offer(frame, resolve)
        return await verdict

    async def wait_for_nodes(self, count: int) -> None:
        """Block until `count` nodes have connected and said HELLO."""
        async def poll():
            while len(self.server.connected_nodes) < count:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), TIMEOUT_S)

    async def _on_message(self, msg: Message, conn: Connection) -> None:
        if msg.type == protocol.HELLO:
            await conn.send(Message(protocol.WELCOME, {"node": msg.node}))
        elif msg.type == protocol.ADD_PEER:
            self.engine.add_peer(msg.node, msg.get("peer"), msg.get("encrypt"), msg.get("lmk"))
        elif msg.type == protocol.DEL_PEER:
            self.engine.del_peer(msg.node, msg.get("peer"))
        elif msg.type == protocol.TX:
            # Guest clocks drift from host time and from each other under stop/cont (D-029), so a
            # QEMU frame starts at the slice it arrived in; a mock's own time is exact.
            start = self.now_us if msg.node in self.qemu_nodes else msg.get("t")
            self._pending.append((Frame(
                src_node=msg.node, src_mac=msg.get("src"), dst_mac=msg.get("dst"),
                channel=msg.get("channel"), rate_mbps=ESPNOW_RATE_MBPS, seq=msg.get("seq"),
                payload=msg.get("data"), start_us=start), None))
        elif msg.type == protocol.SLICE_DONE and msg.get("t") == self._current_until_us:
            # A stale ack from an earlier slice must not release this barrier.
            self._done.add(msg.node)
            if self._done >= self._expected:
                self._barrier.set()

    async def run(self, duration_us: int) -> SimStats:
        """Advance virtual time in slices until `duration_us` is reached."""
        while self.now_us < duration_us:
            until = min(self.now_us + self.slice_us, duration_us)
            self.now_us += await self._run_one_slice(until)
            self.stats.slices += 1
            await self._flush_due()
        return self.stats

    async def _run_one_slice(self, until_us: int) -> int:
        """Run every node through one slice and settle its frames; return the virtual time elapsed."""
        self._done = set()
        self._current_until_us = until_us
        self._barrier.clear()
        self._expected = set(self.server.connected_nodes) - set(self.qemu_nodes)

        await asyncio.gather(*(self.server.send(nid, Message(protocol.RUN_SLICE, {"until_t": until_us}))
                               for nid in self._expected))
        elapsed, _ = await asyncio.gather(self._run_qemu((until_us - self.now_us) / 1e6),
                                          self._wait_for_acks())

        # Frames can land between slices (slirp delivers asynchronously); they join the next batch.
        pending, self._pending = self._pending, []
        if pending:
            frames = [f for f, _ in pending]
            for outcome, (_, sink) in zip(self.engine.process_batch(frames), pending):
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

    async def _wait_for_acks(self) -> None:
        if not self._expected:
            return
        try:
            await asyncio.wait_for(self._barrier.wait(), TIMEOUT_S)
        except asyncio.TimeoutError:
            pass  # a stalled mock must not hang the run; its frames arrive late or never

    def _at(self, t_us: int, deliver: Callable[[], Awaitable[None]]) -> None:
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
            due = delivered[0].delivery_us if delivered else f.start_us + outcome.airtime_us
            self._at(due, lambda: sink(outcome))
            return
        self._at(f.start_us + outcome.airtime_us, lambda: self._send_if_connected(
            Message(protocol.TX_STATUS, {"node": f.src_node, "seq": f.seq, "status": outcome.status})))
        for r in delivered:
            self._at(r.delivery_us, lambda r=r: self._send_if_connected(Message(protocol.RX, {
                "node": r.node_id, "t": r.delivery_us, "src": f.src_mac, "dst": f.dst_mac,
                "channel": f.channel, "rssi_dbm": r.rssi_dbm, "data": f.payload})))

    async def _send_if_connected(self, msg: Message) -> None:
        if msg.node in self.server.connected_nodes:
            await self.server.send(msg.node, msg)


async def _timed(reply: Awaitable) -> float:
    await reply
    return time.monotonic()
