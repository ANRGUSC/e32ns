"""MockNode: a pure-Python virtual ESP32 node speaking the wire protocol (D-000/D-015)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from .. import protocol
from ..protocol import Message, FrameDecoder, encode_frame


@dataclass
class ScheduledTx:
    t_us: int
    dst_mac: str
    payload: bytes
    rate_mbps: float = 1.0
    seq: int = 0
    channel: int = 1
    sent: bool = False


@dataclass
class ReceivedFrame:
    t_us: int
    src_mac: str
    dst_mac: str
    rssi_dbm: float
    data: bytes


class MockNode:
    def __init__(self, node_id: int, mac: str, port: int, channel: int = 1,
                 barrier_ack: bool = True):
        self.node_id = node_id
        self.mac = protocol.normalize_mac(mac)
        self.port = port
        self.channel = channel
        # When False, the node ignores RUN_SLICE and does not send SLICE_DONE — used to
        # stand in for a QEMU node, which is paused and resumed over QMP instead.
        self.barrier_ack = barrier_ack

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._decoder = FrameDecoder()
        self._reader_task: Optional[asyncio.Task] = None
        self._welcomed = asyncio.Event()

        self.schedule: list[ScheduledTx] = []
        self.received: list[ReceivedFrame] = []
        self.send_status: dict[int, str] = {}      # seq -> "success"/"fail"
        self._running = True

    # ---- scripting ----------------------------------------------------------
    def schedule_tx(self, t_us: int, dst_mac: str, payload: bytes,
                    rate_mbps: float = 1.0, seq: Optional[int] = None,
                    channel: Optional[int] = None) -> int:
        seq = seq if seq is not None else len(self.schedule) + 1
        self.schedule.append(ScheduledTx(
            t_us=t_us, dst_mac=protocol.normalize_mac(dst_mac), payload=payload,
            rate_mbps=rate_mbps, seq=seq, channel=channel or self.channel))
        return seq

    # ---- connection ---------------------------------------------------------
    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection("127.0.0.1", self.port)
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._send(protocol.hello(self.node_id, self.mac))
        await asyncio.wait_for(self._welcomed.wait(), timeout=5.0)

    async def close(self) -> None:
        self._running = False
        if self._writer is not None:
            self._writer.close()
        if self._reader_task is not None:
            self._reader_task.cancel()

    async def _send(self, msg: Message) -> None:
        self._writer.write(encode_frame(msg))
        await self._writer.drain()

    # ---- main receive loop --------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while self._running:
                chunk = await self._reader.read(65536)
                if not chunk:
                    return
                for msg in self._decoder.feed(chunk):
                    await self._handle(msg)
        except (asyncio.CancelledError, ConnectionError):
            return

    async def _handle(self, msg: Message) -> None:
        if msg.type == protocol.WELCOME:
            self._welcomed.set()
        elif msg.type == protocol.RUN_SLICE:
            await self._run_slice(msg.get("until_t"))
        elif msg.type == protocol.TX_STATUS:
            self.send_status[msg.get("seq")] = msg.get("status")
        elif msg.type == protocol.RX:
            self.received.append(ReceivedFrame(
                t_us=msg.get("t"), src_mac=msg.get("src"), dst_mac=msg.get("dst"),
                rssi_dbm=msg.get("rssi_dbm"), data=protocol.hex_to_data(msg.get("data"))))
        elif msg.type == protocol.SHUTDOWN:
            await self.close()

    async def emit_due(self, until_t: int) -> None:
        """Send any scheduled transmissions whose virtual time is before ``until_t``."""
        for tx in self.schedule:
            if not tx.sent and tx.t_us < until_t:
                await self._send(protocol.tx(
                    self.node_id, tx.t_us, self.mac, tx.dst_mac, tx.channel,
                    tx.rate_mbps, tx.seq, tx.payload))
                tx.sent = True

    async def _run_slice(self, until_t: int) -> None:
        """Emit due transmissions, then ack the barrier (a QEMU stand-in does neither)."""
        if not self.barrier_ack:
            return
        await self.emit_due(until_t)
        await self._send(protocol.slice_done(self.node_id, until_t))
