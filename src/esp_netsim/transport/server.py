"""Asyncio TCP server carrying the node<->medium protocol; connections keyed by node id (D-003)."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from .. import protocol
from ..protocol import FrameDecoder, Message, encode_frame


class Connection:
    """One node's socket."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    async def send(self, msg: Message) -> None:
        self._writer.write(encode_frame(msg))
        await self._writer.drain()

    async def messages(self):
        """Decoded messages until EOF."""
        decoder = FrameDecoder()
        while chunk := await self._reader.read(65536):
            for msg in decoder.feed(chunk):
                yield msg

    def close(self) -> None:
        self._writer.close()


MessageHandler = Callable[[Message, Connection], Awaitable[None]]


class MediumServer:
    """Loopback TCP server on a free port: hands every inbound message to the handler."""

    def __init__(self, handler: MessageHandler):
        self._handler = handler
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: dict[int, Connection] = {}

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)

    async def _on_client(self, reader, writer) -> None:
        conn = Connection(reader, writer)
        node_id = None
        try:
            async for msg in conn.messages():
                if msg.type == protocol.HELLO and node_id is None:
                    node_id = msg.node
                    self._conns[node_id] = conn
                await self._handler(msg, conn)
        finally:
            self._conns.pop(node_id, None)
            conn.close()

    async def send(self, node_id: int, msg: Message) -> None:
        await self._conns[node_id].send(msg)

    async def broadcast(self, msg: Message) -> None:
        for conn in list(self._conns.values()):
            await conn.send(msg)

    @property
    def connected_nodes(self) -> set[int]:
        return set(self._conns)

    async def stop(self) -> None:
        for conn in list(self._conns.values()):
            conn.close()
        self._server.close()
        await self._server.wait_closed()
