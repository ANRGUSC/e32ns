"""MockNode: a pure-Python virtual ESP32 node speaking the wire protocol (D-015)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from .. import protocol
from ..protocol import Message
from ..transport.server import Connection


@dataclass(frozen=True)
class ScheduledTx:
    t_us: int
    dst_mac: str
    payload: bytes
    seq: int


@dataclass(frozen=True)
class ReceivedFrame:
    t_us: int
    rssi_dbm: float
    data: bytes


class MockNode:
    """Sends its scheduled ESP-NOW frames, acks each slice, and records what reaches it."""

    def __init__(self, node_id: int, mac: str, port: int, channel: int = 1):
        self.node_id = node_id
        self.mac = mac
        self.port = port
        self.channel = channel
        self.schedule: list[ScheduledTx] = []
        self.received: list[ReceivedFrame] = []
        self.send_status: dict[int, str] = {}   # seq -> send-callback status
        self._conn: Optional[Connection] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._welcomed = asyncio.Event()

    def schedule_tx(self, t_us: int, dst_mac: str, payload: bytes, seq: int) -> None:
        """Queue an ESP-NOW frame for virtual time t_us."""
        self.schedule.append(ScheduledTx(t_us, dst_mac, payload, seq))

    async def connect(self) -> None:
        self._conn = Connection(*await asyncio.open_connection("127.0.0.1", self.port))
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._conn.send(Message(protocol.HELLO, {"node": self.node_id, "mac": self.mac}))
        await asyncio.wait_for(self._welcomed.wait(), timeout=5.0)

    async def close(self) -> None:
        # The runner closes every mock, including any that never connected.
        if self._conn is not None:
            self._conn.close()
        if self._reader_task is not None:
            self._reader_task.cancel()

    async def _read_loop(self) -> None:
        try:
            async for msg in self._conn.messages():
                await self._handle(msg)
        except (asyncio.CancelledError, ConnectionError):
            return

    async def _handle(self, msg: Message) -> None:
        if msg.type == protocol.WELCOME:
            self._welcomed.set()
        elif msg.type == protocol.RUN_SLICE:
            await self.emit_due(msg.get("until_t"))
            await self._conn.send(Message(protocol.SLICE_DONE,
                                          {"node": self.node_id, "t": msg.get("until_t")}))
        elif msg.type == protocol.TX_STATUS:
            self.send_status[msg.get("seq")] = msg.get("status")
        elif msg.type == protocol.RX:
            self.received.append(ReceivedFrame(msg.get("t"), msg.get("rssi_dbm"), msg.get("data")))
        elif msg.type == protocol.SHUTDOWN:
            await self.close()

    async def emit_due(self, until_t: int) -> None:
        """Send every scheduled frame due before until_t."""
        due = [tx for tx in self.schedule if tx.t_us < until_t]
        self.schedule = [tx for tx in self.schedule if tx.t_us >= until_t]
        for tx in due:
            await self._conn.send(Message(protocol.TX, {
                "node": self.node_id, "t": tx.t_us, "src": self.mac, "dst": tx.dst_mac,
                "channel": self.channel, "seq": tx.seq, "data": tx.payload}))
