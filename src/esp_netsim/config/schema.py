"""YAML scenario schema + fail-fast validation at the config boundary (brief §9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..protocol import normalize_mac
from ..medium import MediumConfig, MediumEngine
from ..channel.pathloss import LogDistancePropagationLoss, LinkBudget
from ..channel.error_model import ConstantErrorModel, SnrPerErrorModel
from ..channel.airtime import AirtimeModel


@dataclass(frozen=True)
class NodeConfig:
    node_id: int
    mac: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    channel: int = 1


@dataclass(frozen=True)
class MediumConfigSpec:
    error_model: str = "snr_per"          # constant | snr_per
    packet_error_rate: float = 0.0
    path_loss_exponent: float = 3.0
    tx_power_dbm: float = 20.0
    noise_figure_db: float = 7.0
    capture_threshold_db: float = 4.0
    max_retries: int = 3

    def build(self, seed: int) -> MediumConfig:
        if self.error_model == "constant":
            em = ConstantErrorModel(self.packet_error_rate)
        elif self.error_model == "snr_per":
            em = SnrPerErrorModel()
        else:
            raise ValueError(f"medium.error_model must be constant|snr_per, "
                             f"got {self.error_model!r}")
        return MediumConfig(
            propagation=LogDistancePropagationLoss(exponent=self.path_loss_exponent),
            link_budget=LinkBudget(tx_power_dbm=self.tx_power_dbm),
            error_model=em, airtime=AirtimeModel(),
            noise_figure_db=self.noise_figure_db,
            capture_threshold_db=self.capture_threshold_db,
            max_retries=self.max_retries, seed=seed,
        )


@dataclass(frozen=True)
class TrafficConfig:
    """A scripted transmission for a mock-node demo."""

    node_id: int
    at_us: int
    dst: str
    size_bytes: int = 16
    rate_mbps: float = 1.0
    count: int = 1
    period_us: int = 0


@dataclass(frozen=True)
class ScenarioConfig:
    name: str
    nodes: tuple
    medium: MediumConfigSpec = field(default_factory=MediumConfigSpec)
    duration_us: int = 1_000_000
    slice_us: int = 1000
    seed: int = 0
    trace_path: Optional[str] = None
    port: int = 0
    traffic: tuple = ()

    def build_engine(self) -> MediumEngine:
        engine = MediumEngine(self.medium.build(self.seed))
        for n in self.nodes:
            engine.add_node(n.node_id, n.mac, n.x, n.y, n.z, n.channel)
        return engine


def _require(d: dict, key: str, ctx: str):
    if key not in d:
        raise ValueError(f"{ctx}: missing required key '{key}'")
    return d[key]


def parse_scenario(doc: dict) -> ScenarioConfig:
    if not isinstance(doc, dict):
        raise ValueError("scenario must be a mapping at the top level")
    raw_nodes = _require(doc, "nodes", "scenario")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("scenario.nodes must be a non-empty list")

    nodes = []
    seen_ids, seen_macs = set(), set()
    for i, rn in enumerate(raw_nodes):
        ctx = f"nodes[{i}]"
        nid = int(_require(rn, "id", ctx))
        mac = normalize_mac(_require(rn, "mac", ctx))
        if nid in seen_ids:
            raise ValueError(f"{ctx}: duplicate node id {nid}")
        if mac in seen_macs:
            raise ValueError(f"{ctx}: duplicate mac {mac}")
        seen_ids.add(nid)
        seen_macs.add(mac)
        pos = rn.get("pos", [0.0, 0.0, 0.0])
        if not isinstance(pos, (list, tuple)) or len(pos) < 2:
            raise ValueError(f"{ctx}.pos must be [x, y] or [x, y, z]")
        z = float(pos[2]) if len(pos) > 2 else 0.0
        nodes.append(NodeConfig(node_id=nid, mac=mac, x=float(pos[0]), y=float(pos[1]),
                                z=z, channel=int(rn.get("channel", 1))))

    run = doc.get("run", {})
    return ScenarioConfig(
        name=doc.get("name", "scenario"), nodes=tuple(nodes),
        medium=_parse_medium(doc.get("medium", {})),
        duration_us=int(run.get("duration_us", 1_000_000)),
        slice_us=int(run.get("slice_us", 1000)),
        seed=int(run.get("seed", 0)),
        trace_path=run.get("trace_path"),
        port=int(run.get("port", 0)),
        traffic=tuple(_parse_traffic(doc.get("traffic", []), seen_ids)),
    )


def _parse_traffic(raw: Any, valid_ids: set) -> list:
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("scenario.traffic must be a list")
    out = []
    for i, t in enumerate(raw):
        ctx = f"traffic[{i}]"
        nid = int(_require(t, "node", ctx))
        if nid not in valid_ids:
            raise ValueError(f"{ctx}: node {nid} is not a declared node")
        dst = t.get("dst", "broadcast")
        dst = "ff:ff:ff:ff:ff:ff" if dst in ("broadcast", "bcast") else normalize_mac(dst)
        out.append(TrafficConfig(
            node_id=nid, at_us=int(_require(t, "at_us", ctx)), dst=dst,
            size_bytes=int(t.get("size_bytes", 16)),
            rate_mbps=float(t.get("rate_mbps", 1.0)),
            count=int(t.get("count", 1)), period_us=int(t.get("period_us", 0))))
    return out


def _parse_medium(m: dict) -> MediumConfigSpec:
    if not isinstance(m, dict):
        raise ValueError("scenario.medium must be a mapping")
    error_model = m.get("error_model", "snr_per")
    if error_model not in ("constant", "snr_per"):
        raise ValueError(f"medium.error_model must be constant|snr_per, got {error_model!r}")
    return MediumConfigSpec(
        error_model=error_model,
        packet_error_rate=float(m.get("packet_error_rate", 0.0)),
        path_loss_exponent=float(m.get("path_loss_exponent", 3.0)),
        tx_power_dbm=float(m.get("tx_power_dbm", 20.0)),
        noise_figure_db=float(m.get("noise_figure_db", 7.0)),
        capture_threshold_db=float(m.get("capture_threshold_db", 4.0)),
        max_retries=int(m.get("max_retries", 3)))


def load_scenario(path: str) -> ScenarioConfig:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return parse_scenario(yaml.safe_load(f))
