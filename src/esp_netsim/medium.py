"""Wireless medium: geometry, carrier sense, collisions and ESP-NOW semantics, with no I/O (D-012)."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Optional

from .protocol import normalize_mac, is_broadcast
from .channel import units
from .channel.pathloss import LogDistancePropagationLoss, LinkBudget
from .channel.error_model import ErrorRateModel, SnrPerErrorModel, ReceptionContext
from .channel.airtime import AirtimeModel

AP_NODE_ID = 0              # scenario node ids start at 1


@dataclass(frozen=True)
class PeerInfo:
    mac: str
    channel: int = 0            # 0 => "current channel" (brief §3)
    encrypt: bool = False
    lmk: Optional[bytes] = None


@dataclass(frozen=True)
class NodeState:
    node_id: int
    mac: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    channel: int = 1
    espnow: bool = True         # False for the access point, which ignores ESP-NOW
    peers: dict = field(default_factory=dict)  # mac -> PeerInfo

    def distance_to(self, other: "NodeState") -> float:
        return math.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2
                         + (self.z - other.z) ** 2)


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
    wifi: bool = False          # an infrastructure data frame (payload is an Ethernet frame)

    @property
    def broadcast(self) -> bool:
        return is_broadcast(self.dst_mac)


@dataclass
class RxVerdict:
    node_id: int
    delivered: bool
    rssi_dbm: float
    snr_db: float
    per: float
    reason: str
    delivery_us: int


@dataclass
class TxOutcome:
    frame: Frame
    status: str               # "success" | "fail"
    airtime_us: int
    attempts: int
    receivers: list           # list[RxVerdict]
    reason: str = ""          # top-level explanation for the send-callback verdict


@dataclass
class _InFlight:
    frame: Frame
    end_us: int


@dataclass
class MediumConfig:
    propagation: LogDistancePropagationLoss = field(default_factory=LogDistancePropagationLoss)
    link_budget: LinkBudget = field(default_factory=LinkBudget)
    error_model: ErrorRateModel = field(default_factory=SnrPerErrorModel)
    airtime: AirtimeModel = field(default_factory=AirtimeModel)
    noise_figure_db: float = 7.0        # typical Wi-Fi front-end NF
    bandwidth_hz: float = 22_000_000.0  # DSSS channel width (brief §5)
    capture_threshold_db: float = 4.0   # min SINR to capture a frame (brief §3/§5)
    cca_threshold_dbm: float = -82.0    # 802.11 preamble-detect level: at or above, a sender defers
    max_retries: int = 3                # unicast MAC retransmissions (brief §3)
    seed: int = 0


class MediumEngine:
    """Deterministic wireless-medium engine (see module docstring)."""

    def __init__(self, config: Optional[MediumConfig] = None):
        self.cfg = config or MediumConfig()
        self._rng = random.Random(self.cfg.seed)  # one RNG makes a seeded run reproducible
        self._nodes: dict[int, NodeState] = {}
        self._by_mac: dict[str, int] = {}
        self._in_flight: list[_InFlight] = []

    def add_node(self, node_id: int, mac: str, x: float = 0.0, y: float = 0.0,
                 z: float = 0.0, channel: int = 1, espnow: bool = True) -> NodeState:
        mac = normalize_mac(mac)
        ns = NodeState(node_id=node_id, mac=mac, x=x, y=y, z=z, channel=channel, espnow=espnow)
        self._nodes[node_id] = ns
        self._by_mac[mac] = node_id
        return ns

    def add_peer(self, node_id: int, peer_mac: str, channel: int = 0,
                 encrypt: bool = False, lmk: Optional[bytes] = None) -> None:
        node = self._nodes[node_id]
        peer_mac = normalize_mac(peer_mac)
        peers = dict(node.peers)
        peers[peer_mac] = PeerInfo(peer_mac, channel, encrypt, lmk)
        self._nodes[node_id] = replace(node, peers=peers)

    def node(self, node_id: int) -> NodeState:
        return self._nodes[node_id]

    def node_id_for_mac(self, mac: str) -> Optional[int]:
        return self._by_mac.get(normalize_mac(mac))

    def process_batch(self, frames: list[Frame]) -> list[TxOutcome]:
        """Verdicts, in input order, for one slice's frames contending with each other and in-flight ones."""
        airtimes = {idx: self.cfg.airtime.tx_airtime_us(len(f.payload), f.rate_mbps,
                                                         not f.broadcast, self._rng)
                    for idx, f in enumerate(frames)}
        self._prune_in_flight(min((f.start_us for f in frames), default=0))
        frames = self._carrier_sense(frames, airtimes)
        windows = {idx: (f.start_us, f.start_us + airtimes[idx]) for idx, f in enumerate(frames)}

        outcomes: list[TxOutcome] = []
        for idx, f in enumerate(frames):
            others = [(g, windows[j]) for j, g in enumerate(frames) if j != idx]
            others += [(i.frame, (i.frame.start_us, i.end_us)) for i in self._in_flight]
            outcomes.append(self._evaluate_frame(f, airtimes[idx], windows[idx],
                                                 _Interference(f.channel, others)))

        # Register as in-flight ONLY the frames that actually radiated. Frames
        # rejected before transmission (peer_not_exist / channel_mismatch /
        # encrypt_no_matching_lmk) emit no RF energy and must not
        # interfere with later frames (review BUG-1). attempts>0 == "went on the air".
        for idx, outcome in enumerate(outcomes):
            if outcome.attempts > 0:
                self._in_flight.append(_InFlight(frames[idx], windows[idx][1]))
        return outcomes

    def transmit(self, frame: Frame) -> TxOutcome:
        """Convenience: process a single frame (no same-slice peers)."""
        return self.process_batch([frame])[0]

    def _carrier_sense(self, frames: list[Frame], airtimes: dict[int, int]) -> list[Frame]:
        """Delay each frame past any earlier same-channel frame its sender can hear (DCF deferral)."""
        busy = [(i.frame, i.frame.start_us, i.end_us) for i in self._in_flight]
        out = list(frames)
        for idx in sorted(range(len(frames)), key=lambda i: frames[i].start_us):
            f = frames[idx]
            start = f.start_us
            for g, g_start, g_end in sorted(busy, key=lambda b: b[1]):
                if (g.channel == f.channel and g_start <= start < g_end
                        and self._hears(f.src_node, g.src_node)):
                    start = g_end
            out[idx] = replace(f, start_us=start)
            busy.append((f, start, start + airtimes[idx]))
        return out

    def _hears(self, listener: int, talker: int) -> bool:
        d = self._nodes[listener].distance_to(self._nodes[talker])
        power = self.cfg.link_budget.rx_power_dbm(self.cfg.propagation, d)
        return power >= self.cfg.cca_threshold_dbm

    def _prune_in_flight(self, now_us: int) -> None:
        self._in_flight = [i for i in self._in_flight if i.end_us > now_us]

    def _rx_power_dbm(self, src: NodeState, dst: NodeState) -> tuple[float, float]:
        d = src.distance_to(dst)
        prx = self.cfg.link_budget.rx_power_dbm(self.cfg.propagation, d, self._rng)
        return prx, d

    def _sinr_db(self, wanted_dbm: float, interferer_powers_dbm: list[float]) -> float:
        noise_mw = units.dbm_to_mw(units.thermal_noise_dbm(self.cfg.bandwidth_hz,
                                                           self.cfg.noise_figure_db))
        interf_mw = sum(units.dbm_to_mw(p) for p in interferer_powers_dbm)
        return wanted_dbm - units.mw_to_dbm(noise_mw + interf_mw)

    def _reception(self, f: Frame, receiver: NodeState,
                   interferers: list[Frame], delivery_us: int) -> RxVerdict:
        """Compute a single receiver's verdict for frame f."""
        src = self._nodes[f.src_node]
        wanted_dbm, dist = self._rx_power_dbm(src, receiver)
        interf_dbm = []
        for g in interferers:
            gsrc = self._nodes[g.src_node]
            p, _ = self._rx_power_dbm(gsrc, receiver)
            interf_dbm.append(p)
        sinr = self._sinr_db(wanted_dbm, interf_dbm)

        if sinr < self.cfg.capture_threshold_db:
            # Distinguish a genuine collision (interferers present) from a link that is
            # simply too weak vs. noise (review LOW: mislabelled reason).
            reason = "collision" if interf_dbm else "below_sensitivity"
            return RxVerdict(receiver.node_id, False, wanted_dbm, sinr, 1.0,
                             reason, delivery_us)

        ctx = ReceptionContext(snr_db=sinr, nbytes=len(f.payload),
                               rate_mbps=f.rate_mbps, distance_m=dist,
                               bandwidth_hz=self.cfg.bandwidth_hz)
        per = self.cfg.error_model.per(ctx)
        delivered = self._rng.random() >= per
        reason = "ok" if delivered else "snr_loss"
        return RxVerdict(receiver.node_id, delivered, wanted_dbm, sinr, per, reason,
                         delivery_us)

    def _evaluate_frame(self, f: Frame, airtime_us: int,
                        window: tuple[int, int], interference: "_Interference") -> TxOutcome:
        if f.broadcast:
            # Broadcast is unacknowledged, so the whole airtime is the frame (no ACK
            # tail); the data lands at the end of airtime.
            base_delivery = window[0] + airtime_us
            return self._evaluate_broadcast(f, airtime_us, base_delivery,
                                            interference.during(window))
        return self._evaluate_unicast(f, airtime_us, window, interference)

    def _evaluate_broadcast(self, f: Frame, airtime_us: int, base_delivery: int,
                            interferers: list[Frame]) -> TxOutcome:
        receivers: list[RxVerdict] = []
        for nid, node in self._nodes.items():
            if nid == f.src_node:
                continue
            if node.channel != f.channel or not node.espnow:
                continue
            prop = self.cfg.airtime.propagation_delay_us(
                self._nodes[f.src_node].distance_to(node))
            v = self._reception(f, node, interferers, base_delivery + prop)
            if v.delivered:
                receivers.append(v)
            else:
                receivers.append(v)  # keep for tracing (delivered=False)
        # Broadcast is unacknowledged, so the send callback reports success once on the air.
        return TxOutcome(f, "success", airtime_us, 1, receivers, "broadcast_unacked")

    def _evaluate_unicast(self, f: Frame, airtime_us: int,
                          window: tuple[int, int], interference: "_Interference") -> TxOutcome:
        dst_id = self.node_id_for_mac(f.dst_mac)
        if dst_id is None:
            return TxOutcome(f, "fail", airtime_us, 0, [], "peer_not_exist")  # brief §3
        dst = self._nodes[dst_id]
        if dst.channel != f.channel:
            return TxOutcome(f, "fail", airtime_us, 0, [], "channel_mismatch")  # brief §3

        # Encrypted unicast requires the receiver to have the sender as a peer with LMK
        # (policy-level model). We check the *destination* has a peer entry for src.
        src = self._nodes[f.src_node]
        src_peer = src.peers.get(dst.mac)
        if src_peer and src_peer.encrypt:
            dst_peer = dst.peers.get(src.mac)
            if dst_peer is None or dst_peer.lmk != src_peer.lmk:
                return TxOutcome(f, "fail", airtime_us, 0, [], "encrypt_no_matching_lmk")

        # Retry loop: each attempt redraws loss and costs airtime (brief §3).
        # The DATA frame lands `ack_tail` (SIFS+ACK) before the attempt's airtime ends,
        # so the receiver's RX time excludes the ACK tail (review BUG-3); the sender's
        # send-cb (status_t) fires after the ACK, i.e. at full airtime.
        total_airtime = airtime_us
        attempts = 0
        prop = self.cfg.airtime.propagation_delay_us(src.distance_to(dst))
        ack_tail = self.cfg.airtime.ack_tail_us()
        last_v: Optional[RxVerdict] = None
        for attempt in range(self.cfg.max_retries + 1):
            attempts += 1
            # Data lands: elapsed full attempts, minus this attempt's ACK tail.
            delivery = window[0] + total_airtime - ack_tail + prop
            # Each retry goes out after the previous attempt, so it meets different traffic.
            attempt_start = window[0] + total_airtime - airtime_us
            v = self._reception(f, dst, interference.during(
                (attempt_start, attempt_start + airtime_us)), delivery)
            last_v = v
            if v.delivered:
                return TxOutcome(f, "success", total_airtime, attempts, [v], "acked")
            if attempt < self.cfg.max_retries:
                total_airtime += airtime_us  # retransmit costs another airtime
        return TxOutcome(f, "fail", total_airtime, attempts,
                         [last_v] if last_v else [], "lost_on_air")


@dataclass(frozen=True)
class _Interference:
    """Other transmissions a frame could meet, each with its on-air window."""

    channel: int
    others: list

    def during(self, window: tuple[int, int]) -> list[Frame]:
        return [g for g, w in self.others if g.channel == self.channel and _overlaps(window, w)]


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]
