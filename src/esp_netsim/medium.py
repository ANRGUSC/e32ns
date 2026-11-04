"""Wireless medium: geometry, carrier sense, collisions and ESP-NOW semantics, with no I/O (D-012)."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Optional

from .channel import (ACK_TAIL_US, ErrorModel, LogDistance, SnrPerErrorModel, airtime_us,
                      from_db, noise_floor_dbm, propagation_delay_us, to_dbm)
from .protocol import BROADCAST_MAC, STATUS_FAIL, STATUS_SUCCESS

AP_NODE_ID = 0                  # scenario node ids start at 1
CCA_THRESHOLD_DBM = -82.0       # 802.11 preamble detection: a sender that hears this much defers


@dataclass(frozen=True)
class PeerInfo:
    encrypt: bool
    lmk: bytes


@dataclass(frozen=True)
class NodeState:
    node_id: int
    mac: str
    x: float
    y: float
    channel: int
    espnow: bool                # False for the access point, which ignores ESP-NOW
    peers: dict = field(default_factory=dict)   # peer MAC -> PeerInfo

    def distance_to(self, other: NodeState) -> float:
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)


@dataclass(frozen=True)
class Link:
    """A user override for the pair a-b, both directions: fixed RSSI and/or extra loss."""

    a: int
    b: int
    rssi_dbm: Optional[float] = None    # replaces path loss, so it also sets interference and CCA
    loss: float = 0.0                   # per-attempt drop probability on top of the channel's


@dataclass(frozen=True)
class Frame:
    src_node: int
    src_mac: str
    dst_mac: str
    channel: int
    rate_mbps: float
    seq: int
    payload: bytes
    start_us: int
    wifi: bool = False          # an infrastructure frame between a station and the AP
    mgmt: Optional[int] = None  # 802.11 management subtype; None: payload is an Ethernet frame

    @property
    def broadcast(self) -> bool:
        return self.dst_mac == BROADCAST_MAC


@dataclass(frozen=True)
class RxVerdict:
    node_id: int
    delivered: bool
    rssi_dbm: float
    snr_db: float
    per: float
    reason: str
    delivery_us: int


@dataclass(frozen=True)
class TxOutcome:
    frame: Frame
    status: str
    airtime_us: int
    attempts: int
    receivers: list[RxVerdict]
    reason: str


@dataclass
class MediumConfig:
    propagation: LogDistance = field(default_factory=LogDistance)
    error_model: ErrorModel = field(default_factory=SnrPerErrorModel)
    noise_figure_db: float = 7.0        # typical Wi-Fi front end
    capture_threshold_db: float = 4.0   # minimum SINR to capture a frame
    max_retries: int = 34               # unicast retransmissions, measured on an ESP32-S3 (V-014)
    seed: int = 0


class MediumEngine:
    """Decides every frame's fate from geometry, links and overlapping traffic; one seeded RNG."""

    def __init__(self, config: MediumConfig):
        self.cfg = config
        self._rng = random.Random(config.seed)
        self._noise_mw = from_db(noise_floor_dbm(config.noise_figure_db))
        self._nodes: dict[int, NodeState] = {}
        self._by_mac: dict[str, int] = {}
        self._in_flight: list[tuple[Frame, int]] = []   # (frame, end of its last attempt)
        self._links: dict[frozenset, Link] = {}

    def add_node(self, node_id: int, mac: str, x: float = 0.0, y: float = 0.0, channel: int = 1,
                 espnow: bool = True) -> None:
        self._nodes[node_id] = NodeState(node_id, mac, x, y, channel, espnow)
        self._by_mac[mac] = node_id

    def add_peer(self, node_id: int, peer_mac: str, encrypt: bool, lmk: bytes) -> None:
        node = self._nodes[node_id]
        self._nodes[node_id] = replace(node, peers={**node.peers, peer_mac: PeerInfo(encrypt, lmk)})

    def del_peer(self, node_id: int, peer_mac: str) -> None:
        node = self._nodes[node_id]
        peers = {mac: p for mac, p in node.peers.items() if mac != peer_mac}
        self._nodes[node_id] = replace(node, peers=peers)

    def set_link(self, link: Link) -> None:
        self._links[frozenset((link.a, link.b))] = link

    def node(self, node_id: int) -> NodeState:
        return self._nodes[node_id]

    def process_batch(self, frames: list[Frame]) -> list[TxOutcome]:
        """Verdicts, in input order, for one slice's frames contending with each other and in-flight ones."""
        airtimes = [airtime_us(len(f.payload), f.rate_mbps, not f.broadcast, self._rng)
                    for f in frames]
        earliest = min(f.start_us for f in frames)
        self._in_flight = [(g, end) for g, end in self._in_flight if end > earliest]
        frames = self._carrier_sense(frames, airtimes)
        windows = [(f.start_us, f.start_us + a) for f, a in zip(frames, airtimes)]

        outcomes = []
        for idx, f in enumerate(frames):
            others = [(g, windows[j]) for j, g in enumerate(frames) if j != idx]
            others += [(g, (g.start_us, end)) for g, end in self._in_flight]
            others = [(g, w) for g, w in others if g.channel == f.channel]
            outcomes.append(self._evaluate_broadcast(f, airtimes[idx], others) if f.broadcast
                            else self._evaluate_unicast(f, airtimes[idx], others))

        # A frame rejected before transmission (channel_mismatch, encrypt_no_matching_lmk) emits
        # nothing; one that radiated holds the air for all its attempts.
        self._in_flight += [(f, f.start_us + o.airtime_us)
                            for f, o in zip(frames, outcomes) if o.attempts > 0]
        return outcomes

    def transmit(self, frame: Frame) -> TxOutcome:
        """Process one frame on its own."""
        return self.process_batch([frame])[0]

    def link_quality(self, src_id: int, dst_id: int, nbytes: int,
                     rate_mbps: float) -> tuple[float, float]:
        """Mean RSSI and packet error rate of one attempt on a link with no interference."""
        rssi = self._rx_power(self._nodes[src_id], self._nodes[dst_id])
        sinr = self._sinr_db(rssi, [])
        per = 1.0 if sinr < self.cfg.capture_threshold_db else self.cfg.error_model.per(
            sinr, nbytes, rate_mbps)
        return rssi, 1.0 - (1.0 - per) * (1.0 - self._link_loss(src_id, dst_id))

    def _carrier_sense(self, frames: list[Frame], airtimes: list[int]) -> list[Frame]:
        """Delay each frame past any earlier same-channel frame its sender can hear (DCF deferral)."""
        busy = [(g, g.start_us, end) for g, end in self._in_flight]
        out = list(frames)
        for idx in sorted(range(len(frames)), key=lambda i: frames[i].start_us):
            f = frames[idx]
            start = f.start_us
            for g, g_start, g_end in sorted(busy, key=lambda b: b[1]):
                if (g.channel == f.channel and g_start <= start < g_end
                        and self._rx_power(self._nodes[g.src_node], self._nodes[f.src_node])
                        >= CCA_THRESHOLD_DBM):
                    start = g_end
            out[idx] = replace(f, start_us=start)
            busy.append((f, start, start + airtimes[idx]))
        return out

    def _rx_power(self, src: NodeState, dst: NodeState) -> float:
        link = self._links.get(frozenset((src.node_id, dst.node_id)))
        if link and link.rssi_dbm is not None:
            return link.rssi_dbm
        return self.cfg.propagation.rx_power_dbm(src.distance_to(dst))

    def _link_loss(self, a: int, b: int) -> float:
        link = self._links.get(frozenset((a, b)))
        return link.loss if link else 0.0

    def _sinr_db(self, wanted_dbm: float, interferer_powers_dbm: list[float]) -> float:
        interf_mw = sum(from_db(p) for p in interferer_powers_dbm)
        return wanted_dbm - to_dbm(self._noise_mw + interf_mw)

    def _reception(self, f: Frame, receiver: NodeState, interferers: list[Frame],
                   delivery_us: int) -> RxVerdict:
        wanted_dbm = self._rx_power(self._nodes[f.src_node], receiver)
        interf_dbm = [self._rx_power(self._nodes[g.src_node], receiver) for g in interferers]
        sinr = self._sinr_db(wanted_dbm, interf_dbm)
        if sinr < self.cfg.capture_threshold_db:
            # Interference is to blame only if the frame would have been received without it.
            heard_alone = self._sinr_db(wanted_dbm, []) >= self.cfg.capture_threshold_db
            reason = "collision" if interf_dbm and heard_alone else "below_sensitivity"
            return RxVerdict(receiver.node_id, False, wanted_dbm, sinr, 1.0, reason, delivery_us)
        per = self.cfg.error_model.per(sinr, len(f.payload), f.rate_mbps)
        loss = self._link_loss(f.src_node, receiver.node_id)
        if self._rng.random() < per:
            reason = "snr_loss"
        elif loss and self._rng.random() < loss:  # no draw without a link: seeded runs unchanged
            reason = "link_loss"
        else:
            reason = "ok"
        return RxVerdict(receiver.node_id, reason == "ok", wanted_dbm, sinr,
                         1.0 - (1.0 - per) * (1.0 - loss), reason, delivery_us)

    def _evaluate_broadcast(self, f: Frame, airtime: int, others: list) -> TxOutcome:
        # Unacknowledged: the data lands at the end of the airtime, and the send callback
        # reports success once the frame is on the air.
        window = (f.start_us, f.start_us + airtime)
        interferers = _overlapping(others, window)
        src = self._nodes[f.src_node]
        receivers = [self._reception(f, node, interferers,
                                     window[1] + propagation_delay_us(src.distance_to(node)))
                     for nid, node in self._nodes.items()
                     if nid != f.src_node and node.channel == f.channel and node.espnow]
        return TxOutcome(f, STATUS_SUCCESS, airtime, 1, receivers, "broadcast_unacked")

    def _evaluate_unicast(self, f: Frame, airtime: int, others: list) -> TxOutcome:
        dst_id = self._by_mac.get(f.dst_mac)
        if dst_id is None:
            # The radio cannot know nobody is there: it sends every attempt and hears no ACK.
            attempts = self.cfg.max_retries + 1
            return TxOutcome(f, STATUS_FAIL, airtime * attempts, attempts, [], "no_receiver")
        dst = self._nodes[dst_id]
        if dst.channel != f.channel:
            return TxOutcome(f, STATUS_FAIL, airtime, 0, [], "channel_mismatch")

        # Encryption is modeled as policy: the receiver must hold the sender as a peer, same LMK.
        src = self._nodes[f.src_node]
        src_peer = src.peers.get(dst.mac)
        if src_peer and src_peer.encrypt:
            dst_peer = dst.peers.get(src.mac)
            if dst_peer is None or dst_peer.lmk != src_peer.lmk:
                return TxOutcome(f, STATUS_FAIL, airtime, 0, [], "encrypt_no_matching_lmk")

        # Each attempt follows the last, so it meets different traffic and redraws loss. The data
        # lands one ACK tail before the attempt ends; the send callback fires at its end.
        prop = propagation_delay_us(src.distance_to(dst))
        for attempt in range(1, self.cfg.max_retries + 2):
            start = f.start_us + (attempt - 1) * airtime
            v = self._reception(f, dst, _overlapping(others, (start, start + airtime)),
                                start + airtime - ACK_TAIL_US + prop)
            if v.delivered:
                return TxOutcome(f, STATUS_SUCCESS, attempt * airtime, attempt, [v], "acked")
        return TxOutcome(f, STATUS_FAIL, attempt * airtime, attempt, [v], "lost_on_air")


def _overlapping(others: list, window: tuple[int, int]) -> list[Frame]:
    return [g for g, w in others if window[0] < w[1] and w[0] < window[1]]
