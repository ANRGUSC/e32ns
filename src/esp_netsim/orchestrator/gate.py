"""QMP gate: run a real QEMU node one virtual-time slice, then pause it (D-004/D-026)."""

from __future__ import annotations

import asyncio


class QmpGate:
    """Gate a QEMU node via QMP cont/stop; one slice = cont, run slice_us wall, stop.

    The guest clock freezes on stop (V-006), so slice_us of wall advances ~slice_us of
    virtual time. ``qmp`` exposes async cont()/stop().
    """

    def __init__(self, node_id: int, qmp, slice_us: int):
        self.node_id = node_id
        self._qmp = qmp
        self._slice_wall_s = slice_us / 1_000_000.0

    async def release(self, until_us: int) -> None:
        await self._qmp.cont()

    async def wait_barrier(self) -> None:
        await asyncio.sleep(self._slice_wall_s)
        await self._qmp.stop()
