"""Async QMP client for gating a QEMU node via stop/cont over a UNIX socket (D-004/D-024).

QMP must ride a UNIX socket, never TCP: TCP QMP stalls ~88 ms/cycle on delayed-ACK vs
~0.75 ms over a UNIX socket (V-007).
"""

from __future__ import annotations

import asyncio
import json


class QmpClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._r = reader
        self._w = writer

    @classmethod
    async def connect_unix(cls, path: str) -> "QmpClient":
        reader, writer = await asyncio.open_unix_connection(path)
        client = cls(reader, writer)
        await client._negotiate()
        return client

    async def _readline_json(self) -> dict:
        line = await self._r.readline()
        return json.loads(line.decode("utf-8")) if line else {}

    async def _negotiate(self) -> None:
        await self._readline_json()  # greeting
        await self._execute("qmp_capabilities")

    async def _execute(self, command: str) -> dict:
        self._w.write((json.dumps({"execute": command}) + "\r\n").encode("utf-8"))
        await self._w.drain()
        while True:
            resp = await self._readline_json()
            if not resp or "return" in resp or "error" in resp:
                return resp

    async def cont(self) -> dict:
        return await self._execute("cont")

    async def stop(self) -> dict:
        return await self._execute("stop")
