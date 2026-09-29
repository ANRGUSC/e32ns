"""Carry a Wi-Fi station's Ethernet frames over the modeled link to the AP, then on to slirp NAT (D-030)."""

from __future__ import annotations

import asyncio
import itertools
import struct
from dataclasses import dataclass

from ..config.schema import AccessPoint, NodeConfig
from ..medium import Frame
from .simulation import Simulation

WIFI_RATE_MBPS = 11.0
SLIRP_HOST = bytes((10, 0, 2, 2))
LENGTH = struct.Struct(">I")


def is_medium_control(eth: bytes, port: int) -> bool:
    """True for the shim's TCP link to the medium, which is not radio traffic."""
    if len(eth) < 54 or eth[12:14] != b"\x08\x00" or eth[23] != 6:
        return False
    ip = 14
    tcp = ip + (eth[ip] & 0x0F) * 4
    src_port, dst_port = struct.unpack_from(">HH", eth, tcp)
    return ((eth[ip + 16:ip + 20] == SLIRP_HOST and dst_port == port)
            or (eth[ip + 12:ip + 16] == SLIRP_HOST and src_port == port))


@dataclass(frozen=True)
class Hop:
    """One direction of the station-AP link."""

    src_node: int
    src_mac: str
    dst_mac: str
    channel: int


async def bridge(sim: Simulation, node: NodeConfig, ap: AccessPoint, sockets: dict) -> list:
    """Connect to a node's filter sockets and pump both directions; return the pump tasks."""
    conns = {k: await asyncio.open_unix_connection(str(p)) for k, p in sockets.items()}
    up = Hop(node.node_id, node.mac, ap.mac, ap.channel)
    down = Hop(ap.node_id, ap.mac, node.mac, ap.channel)
    return [asyncio.create_task(_pump(sim, conns["up_out"], conns["up_in"], up)),
            asyncio.create_task(_pump(sim, conns["dn_out"], conns["dn_in"], down))]


async def _pump(sim: Simulation, source: tuple, sink: tuple, hop: Hop) -> None:
    # Hold both halves of each connection: a collected StreamWriter closes its socket.
    reader, _ = source
    _, writer = sink
    seq = itertools.count(1)
    while True:
        (n,) = LENGTH.unpack(await reader.readexactly(LENGTH.size))
        eth = await reader.readexactly(n)

        async def forward(eth=eth):
            writer.write(LENGTH.pack(len(eth)) + eth)
            await writer.drain()

        if is_medium_control(eth, sim.server.actual_port):
            await forward()
            continue
        sim.offer(Frame(src_node=hop.src_node, src_mac=hop.src_mac, dst_mac=hop.dst_mac,
                        channel=hop.channel, rate_mbps=WIFI_RATE_MBPS, seq=next(seq),
                        payload=eth, start_us=sim.now_us, wifi=True), forward)
