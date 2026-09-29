"""Asyncio TCP server carrying the node<->medium protocol; connections keyed by node id (D-003)."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from .. import protocol
from ..protocol import Message, FrameDecoder, encode_frame

MessageHandler = Callable[[int, Message, "Connection"], Awaitable[None]]


class Connection:
    """One node's socket connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._decoder = FrameDecoder()
        self.node_id: Optional[int] = None

    @property
    def peername(self):
        return self._writer.get_extra_info("peername")

    async def send(self, msg: Message) -> None:
        self._writer.write(encode_frame(msg))
        await self._writer.drain()

    async def read_messages(self):
        """Async generator yielding decoded Messages until EOF."""
        while True:
            chunk = await self._reader.read(65536)
            if not chunk:
                return
            for msg in self._decoder.feed(chunk):
                yield msg

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:
            pass


class MediumServer:
    """TCP server that accepts node connections and routes messages.

    The orchestrator supplies an async `handler(node_id, msg, conn)` called for
    every inbound message. `send(node_id, msg)` delivers to a specific node.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 52333):
        self.host = host
        self.port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: dict[int, Connection] = {}
        self._pending: set[Connection] = set()
        self._handler: Optional[MessageHandler] = None
        self.on_connect: Optional[Callable[[Connection], Awaitable[None]]] = None
        self.on_disconnect: Optional[Callable[[int], Awaitable[None]]] = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    @property
    def actual_port(self) -> int:
        if self._server is None:
            return self.port
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)

    async def _on_client(self, reader, writer) -> None:
        conn = Connection(reader, writer)
        self._pending.add(conn)
        if self.on_connect:
            await self.on_connect(conn)
        try:
            async for msg in conn.read_messages():
                # First HELLO binds the connection to a node id.
                if msg.type == protocol.HELLO and conn.node_id is None:
                    conn.node_id = msg.node
                    self._conns[conn.node_id] = conn
                    self._pending.discard(conn)
                if self._handler is not None:
                    await self._handler(conn.node_id, msg, conn)
        finally:
            if conn.node_id is not None:
                self._conns.pop(conn.node_id, None)
                if self.on_disconnect:
                    await self.on_disconnect(conn.node_id)
            self._pending.discard(conn)
            conn.close()

    async def send(self, node_id: int, msg: Message) -> None:
        conn = self._conns.get(node_id)
        if conn is None:
            raise KeyError(f"no connected node {node_id}")
        await conn.send(msg)

    async def broadcast(self, msg: Message) -> None:
        for conn in list(self._conns.values()):
            await conn.send(msg)

    @property
    def connected_nodes(self) -> list[int]:
        return sorted(self._conns.keys())

    async def stop(self) -> None:
        for conn in list(self._conns.values()):
            conn.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
