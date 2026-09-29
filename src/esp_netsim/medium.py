"""RadioMedium engine: geometry + physics + ESP-NOW send/recv semantics (D-009/D-012/D-013).

Pure and deterministic given (config, seed, submitted frames); no I/O. The orchestrator
drives it and the transport carries the results.
"""

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

ESP_NOW_MAX_DATA_LEN = 250  # v1.0; v2.0 is 1470 (needs IDF>=5.4.2)


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
    wifi_started: bool = True   # esp_wifi_start must precede TX (brief §3)
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
    max_retries: int = 3                # unicast MAC retransmissions (brief §3, D-012)
    max_data_len: int = ESP_NOW_MAX_DATA_LEN
    seed: int = 0


class MediumEngine:
    """Deterministic wireless-medium engine (see module docstring)."""

    def __init__(self, config: Optional[MediumConfig] = None):
        self.cfg = config or MediumConfig()
        self._rng = random.Random(self.cfg.seed)  # single seeded RNG (D-011)
        self._nodes: dict[int, NodeState] = {}
        self._by_mac: dict[str, int] = {}
        self._in_flight: list[_InFlight] = []

    def add_node(self, node_id: int, mac: str, x: float = 0.0, y: float = 0.0,
                 z: float = 0.0, channel: int = 1) -> NodeState:
        mac = normalize_mac(mac)
        ns = NodeState(node_id=node_id, mac=mac, x=x, y=y, z=z, channel=channel)
        self._nodes[node_id] = ns
        self._by_mac[mac] = node_id
        return ns

    def set_position(self, node_id: int, x: float, y: float, z: float = 0.0) -> None:
        self._nodes[node_id] = replace(self._nodes[node_id], x=x, y=y, z=z)

    def set_channel(self, node_id: int, channel: int) -> None:
        self._nodes[node_id] = replace(self._nodes[node_id], channel=channel)

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
        """Evaluate a set of frames offered around the same virtual time.

        Frames in the batch interfere with one another and with still-in-flight
        frames from earlier slices (D-013). Returns one TxOutcome per frame,
        in input order.
        """
        # Pre-compute airtime windows for every frame in the batch.
        windows: dict[int, tuple[int, int]] = {}
        airtimes: dict[int, int] = {}
        for idx, f in enumerate(frames):
            acknowledged = not f.broadcast
            at = self.cfg.airtime.tx_airtime_us(len(f.payload), f.rate_mbps,
                                                acknowledged, self._rng)
            airtimes[idx] = at
            windows[idx] = (f.start_us, f.start_us + at)

        self._prune_in_flight(min((f.start_us for f in frames), default=0))

        outcomes: list[TxOutcome] = []
        for idx, f in enumerate(frames):
            interferers = self._interferers_for(idx, frames, windows)
            outcomes.append(self._evaluate_frame(f, airtimes[idx], windows[idx],
                                                 interferers))

        # Register as in-flight ONLY the frames that actually radiated. Frames
        # rejected before transmission (peer_not_exist / channel_mismatch /
        # wifi_not_started / encrypt_no_matching_lmk) emit no RF energy and must not
        # interfere with later frames (review BUG-1). attempts>0 == "went on the air".
        for idx, outcome in enumerate(outcomes):
            if outcome.attempts > 0:
                self._in_flight.append(_InFlight(frames[idx], windows[idx][1]))
        return outcomes

    def transmit(self, frame: Frame) -> TxOutcome:
        """Convenience: process a single frame (no same-slice peers)."""
        return self.process_batch([frame])[0]

    def _prune_in_flight(self, now_us: int) -> None:
        self._in_flight = [i for i in self._in_flight if i.end_us > now_us]

    def _interferers_for(self, idx: int, frames: list[Frame],
                         windows: dict[int, tuple[int, int]]) -> list[Frame]:
        """Same-channel frames whose window overlaps frame idx's window (D-013)."""
        f = frames[idx]
        w = windows[idx]
        out: list[Frame] = []
        for j, g in enumerate(frames):
            if j == idx:
                continue
            if g.channel != f.channel:
                continue
            if _overlaps(w, windows[j]):
                out.append(g)
        for infl in self._in_flight:
            if infl.frame.channel == f.channel and _overlaps(w, (infl.frame.start_us,
                                                                 infl.end_us)):
                out.append(infl.frame)
        return out

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
                        window: tuple[int, int], interferers: list[Frame]) -> TxOutcome:
        src = self._nodes[f.src_node]

        # Precondition: Wi-Fi must be started to transmit (brief §3).
        if not src.wifi_started:
            return TxOutcome(f, "fail", airtime_us, 0, [], "wifi_not_started")

        if f.broadcast:
            # Broadcast is unacknowledged, so the whole airtime is the frame (no ACK
            # tail); the data lands at the end of airtime.
            base_delivery = window[0] + airtime_us
            return self._evaluate_broadcast(f, airtime_us, base_delivery, interferers)
        return self._evaluate_unicast(f, airtime_us, window, interferers)

    def _evaluate_broadcast(self, f: Frame, airtime_us: int, base_delivery: int,
                            interferers: list[Frame]) -> TxOutcome:
        receivers: list[RxVerdict] = []
        for nid, node in self._nodes.items():
            if nid == f.src_node:
                continue
            if node.channel != f.channel:
                continue  # channel mismatch: not received
            prop = self.cfg.airtime.propagation_delay_us(
                self._nodes[f.src_node].distance_to(node))
            v = self._reception(f, node, interferers, base_delivery + prop)
            if v.delivered:
                receivers.append(v)
            else:
                receivers.append(v)  # keep for tracing (delivered=False)
        # Broadcast: unacknowledged -> success once transmitted (brief §3, D-012).
        return TxOutcome(f, "success", airtime_us, 1, receivers, "broadcast_unacked")

    def _evaluate_unicast(self, f: Frame, airtime_us: int,
                          window: tuple[int, int], interferers: list[Frame]) -> TxOutcome:
        dst_id = self.node_id_for_mac(f.dst_mac)
        if dst_id is None:
            return TxOutcome(f, "fail", airtime_us, 0, [], "peer_not_exist")  # brief §3
        dst = self._nodes[dst_id]
        if dst.channel != f.channel:
            return TxOutcome(f, "fail", airtime_us, 0, [], "channel_mismatch")  # brief §3

        # Encrypted unicast requires the receiver to have the sender as a peer with LMK
        # (policy-level model, D-012). We check the *destination* has a peer entry for src.
        src = self._nodes[f.src_node]
        src_peer = src.peers.get(dst.mac)
        if src_peer and src_peer.encrypt:
            dst_peer = dst.peers.get(src.mac)
            if dst_peer is None or dst_peer.lmk != src_peer.lmk:
                return TxOutcome(f, "fail", airtime_us, 0, [], "encrypt_no_matching_lmk")

        # Retry loop: each attempt redraws loss and costs airtime (brief §3, D-012).
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
            v = self._reception(f, dst, interferers, delivery)
            last_v = v
            if v.delivered:
                return TxOutcome(f, "success", total_airtime, attempts, [v], "acked")
            if attempt < self.cfg.max_retries:
                total_airtime += airtime_us  # retransmit costs another airtime
        return TxOutcome(f, "fail", total_airtime, attempts,
                         [last_v] if last_v else [], "lost_on_air")


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]
