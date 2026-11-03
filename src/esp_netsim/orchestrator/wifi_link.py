"""Carry a Wi-Fi station over the modeled link to the AP: associate, then Ethernet to slirp."""

from __future__ import annotations

import asyncio
import itertools
import struct
from dataclasses import dataclass, field
from typing import Iterator, Optional

from ..config.schema import AccessPoint, NodeConfig
from ..medium import Frame, TxOutcome
from .simulation import Simulation

WIFI_RATE_MBPS = 11.0
MGMT_RATE_MBPS = 1.0               # management frames go at the lowest basic rate
SLIRP_HOST = bytes((10, 0, 2, 2))
LENGTH = struct.Struct(">I")

# The shim's connect request and the bridge's reply use the IEEE local experimental ethertype,
# so they share the station's NIC without reaching slirp.
ETHERTYPE_CTRL = bytes((0x88, 0xB5))
OP_CONNECT, OP_CONNECT_REPLY = 1, 2

ASSOC_REQ, ASSOC_RESP, PROBE_REQ, PROBE_RESP, AUTH = 0, 1, 4, 5, 11   # 802.11 mgmt subtypes
ASSOCIATED, AUTH_EXPIRE, ASSOC_EXPIRE, NO_AP_FOUND = 0, 2, 4, 201     # esp_wifi reason codes
STEPS = ((PROBE_REQ, PROBE_RESP, NO_AP_FOUND), (AUTH, AUTH, AUTH_EXPIRE),
         (ASSOC_REQ, ASSOC_RESP, ASSOC_EXPIRE))
RATES_IE = bytes((1, 8, 0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24))
ESS = 0x0001


def is_medium_control(eth: bytes, port: int) -> bool:
    """True for the shim's TCP link to the medium, which is not radio traffic."""
    if len(eth) < 54 or eth[12:14] != b"\x08\x00" or eth[23] != 6:
        return False
    ip = 14
    tcp = ip + (eth[ip] & 0x0F) * 4
    src_port, dst_port = struct.unpack_from(">HH", eth, tcp)
    return ((eth[ip + 16:ip + 20] == SLIRP_HOST and dst_port == port)
            or (eth[ip + 12:ip + 16] == SLIRP_HOST and src_port == port))


def parse_connect_request(eth: bytes) -> str:
    """The SSID a station's connect request asks for."""
    if len(eth) < 16 or eth[14] != OP_CONNECT or len(eth) < 16 + eth[15]:
        raise ValueError("malformed connect request")
    return eth[16:16 + eth[15]].decode("utf-8", "replace")


def connect_reply(sta_mac: str, ap_mac: str, reason: int, channel: int, rssi_dbm: float) -> bytes:
    """The bridge's answer to a connect request; the shim turns it into a Wi-Fi event."""
    rssi = max(-128, min(127, round(rssi_dbm)))
    return (_mac(sta_mac) + _mac(ap_mac) + ETHERTYPE_CTRL
            + bytes((OP_CONNECT_REPLY, reason, channel)) + rssi.to_bytes(1, "big", signed=True))


def _mac(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


@dataclass(frozen=True)
class Hop:
    """One direction of the station-AP link."""

    src_node: int
    src_mac: str
    dst_mac: str
    channel: int
    # 802.11 numbers a transmitter's management and data frames from one counter.
    seq: Iterator[int] = field(default_factory=lambda: itertools.count(1), compare=False)

    def frame(self, now_us: int, payload: bytes, mgmt: Optional[int] = None) -> Frame:
        return Frame(src_node=self.src_node, src_mac=self.src_mac, dst_mac=self.dst_mac,
                     channel=self.channel, seq=next(self.seq), payload=payload, start_us=now_us,
                     rate_mbps=WIFI_RATE_MBPS if mgmt is None else MGMT_RATE_MBPS,
                     wifi=True, mgmt=mgmt)


@dataclass(frozen=True)
class StationLink:
    """A station, its AP, and the two directions between them."""

    node: NodeConfig
    ap: AccessPoint
    up: Hop
    down: Hop


def station_link(node: NodeConfig, ap: AccessPoint) -> StationLink:
    """Both directions of the radio link between a station and the AP."""
    return StationLink(node, ap, Hop(node.node_id, node.mac, ap.mac, ap.channel),
                       Hop(ap.node_id, ap.mac, node.mac, ap.channel))


def _mgmt_body(subtype: int, ap: AccessPoint, aid: int) -> bytes:
    ssid = ap.ssid.encode()
    ssid_ie = bytes((0, len(ssid))) + ssid
    if subtype == PROBE_REQ:
        return ssid_ie + RATES_IE
    if subtype == PROBE_RESP:
        return struct.pack("<QHH", 0, 100, ESS) + ssid_ie + RATES_IE + bytes((3, 1, ap.channel))
    if subtype == ASSOC_REQ:
        return struct.pack("<HH", ESS, 10) + ssid_ie + RATES_IE
    return struct.pack("<HHH", ESS, 0, 0xC000 | aid) + RATES_IE


async def associate(sim: Simulation, link: StationLink, ssid: str) -> tuple[int, float]:
    """Probe, authenticate and associate over the modeled link; return (reason, rssi_dbm)."""
    rssi = -127.0

    async def send(hop: Hop, subtype: int) -> TxOutcome:
        # Authentication frames carry their transaction number: 1 from the station, 2 back.
        body = (struct.pack("<HHH", 0, 1 if hop is link.up else 2, 0) if subtype == AUTH
                else _mgmt_body(subtype, link.ap, link.node.node_id))
        return await sim.exchange(hop.frame(sim.now_us, body, subtype))

    for req, resp, failure in STEPS:
        out = await send(link.up, req)
        if out.status != "success" or (req == PROBE_REQ and ssid != link.ap.ssid):
            return failure, rssi
        out = await send(link.down, resp)
        if out.status != "success":
            return failure, rssi
        rssi = out.receivers[0].rssi_dbm
    return ASSOCIATED, rssi


async def bridge(sim: Simulation, node: NodeConfig, ap: AccessPoint, sockets: dict) -> list:
    """Connect to a node's filter sockets and pump both directions; return the pump tasks."""
    conns = {k: await asyncio.open_unix_connection(str(p)) for k, p in sockets.items()}
    link = station_link(node, ap)

    async def on_connect(eth: bytes) -> None:
        reason, rssi = await associate(sim, link, parse_connect_request(eth))
        await _write(conns["dn_in"][1], connect_reply(node.mac, ap.mac, reason, ap.channel, rssi))

    return [asyncio.create_task(_pump(sim, conns["up_out"], conns["up_in"], link.up, on_connect)),
            asyncio.create_task(_pump(sim, conns["dn_out"], conns["dn_in"], link.down, None))]


async def _write(writer: asyncio.StreamWriter, eth: bytes) -> None:
    writer.write(LENGTH.pack(len(eth)) + eth)
    await writer.drain()


async def _pump(sim: Simulation, source: tuple, sink: tuple, hop: Hop, on_connect) -> None:
    # Hold both halves of each connection: a collected StreamWriter closes its socket.
    reader, _ = source
    _, writer = sink
    while True:
        (n,) = LENGTH.unpack(await reader.readexactly(LENGTH.size))
        eth = await reader.readexactly(n)

        async def forward(outcome: TxOutcome, eth=eth) -> None:
            if any(r.delivered for r in outcome.receivers):
                await _write(writer, eth)

        if on_connect and eth[12:14] == ETHERTYPE_CTRL:
            await on_connect(eth)
        elif is_medium_control(eth, sim.server.actual_port):
            await _write(writer, eth)
        else:
            sim.offer(hop.frame(sim.now_us, eth), forward)
