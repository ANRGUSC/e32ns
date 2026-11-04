"""YAML scenario: parsed and validated once at load, then trusted."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import yaml

from ..channel import ConstantErrorModel, LogDistance, SnrPerErrorModel
from ..medium import AP_NODE_ID, Link, MediumConfig, MediumEngine
from ..protocol import BROADCAST_MAC, normalize_mac


@dataclass(frozen=True)
class NodeConfig:
    node_id: int
    mac: str
    x: float
    y: float
    channel: int
    firmware: Optional[str]     # ESP-IDF build dir; None runs a MockNode
    wifi: bool                  # its IP traffic crosses the modeled link to the AP


@dataclass(frozen=True)
class AccessPoint:
    mac: str
    x: float
    y: float
    channel: int
    ssid: str


@dataclass(frozen=True)
class TrafficConfig:
    """A scripted transmission for a mock node."""

    node_id: int
    at_us: int
    dst: str
    size_bytes: int
    count: int
    period_us: int


@dataclass(frozen=True)
class ScenarioConfig:
    name: str
    nodes: tuple
    medium: MediumConfig
    duration_us: int
    slice_us: int
    seed: int
    out_dir: str
    traffic: tuple
    ap: Optional[AccessPoint]
    links: tuple

    def build_engine(self) -> MediumEngine:
        engine = MediumEngine(self.medium)
        for n in self.nodes:
            engine.add_node(n.node_id, n.mac, n.x, n.y, n.channel)
        if self.ap:
            engine.add_node(AP_NODE_ID, self.ap.mac, self.ap.x, self.ap.y, self.ap.channel,
                            espnow=False)
        for link in self.links:
            engine.set_link(link)
        return engine


def _section(d: Any, ctx: str, allowed: set) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"{ctx} must be a mapping")
    unknown = set(d) - allowed
    if unknown:
        raise ValueError(f"{ctx}: unknown key '{sorted(unknown)[0]}'")
    return d


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise ValueError(f"{ctx}: missing required key '{key}'")
    return d[key]


def _positive(value: Any, ctx: str) -> int:
    n = int(value)
    if n < 1:
        raise ValueError(f"{ctx} must be at least 1, got {n}")
    return n


def parse_scenario(doc: Any, base_dir: str = ".") -> ScenarioConfig:
    doc = _section(doc, "scenario", {"name", "nodes", "wifi_ap", "links", "medium", "run", "traffic"})
    raw_nodes = _require(doc, "nodes", "scenario")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("scenario.nodes must be a non-empty list")
    nodes = [_parse_node(rn, f"nodes[{i}]", base_dir) for i, rn in enumerate(raw_nodes)]
    for kind, values in (("node id", [n.node_id for n in nodes]), ("mac", [n.mac for n in nodes])):
        dupes = {v for v in values if values.count(v) > 1}
        if dupes:
            raise ValueError(f"scenario.nodes: duplicate {kind} {sorted(dupes)[0]}")
    ap = _parse_ap(doc.get("wifi_ap"), nodes)
    run = _section(doc.get("run", {}), "run", {"duration_us", "slice_us", "seed", "out_dir"})
    name = doc.get("name", "scenario")
    seed = int(run.get("seed", 0))
    ids = {n.node_id for n in nodes} | ({AP_NODE_ID} if ap else set())
    return ScenarioConfig(
        name=name, nodes=tuple(nodes),
        medium=_parse_medium(doc.get("medium", {}), seed),
        duration_us=_positive(run.get("duration_us", 1_000_000), "run.duration_us"),
        slice_us=_positive(run.get("slice_us", 1000), "run.slice_us"),
        seed=seed,
        out_dir=run.get("out_dir", os.path.join("out", name)),
        traffic=tuple(_parse_traffic(doc.get("traffic", []),
                                     {n.node_id for n in nodes if n.firmware is None})),
        ap=ap,
        links=tuple(_parse_links(doc.get("links", []), ids)),
    )


def _parse_node(rn: Any, ctx: str, base_dir: str) -> NodeConfig:
    rn = _section(rn, ctx, {"id", "mac", "pos", "channel", "firmware", "wifi"})
    nid = int(_require(rn, "id", ctx))
    if nid < 1:
        raise ValueError(f"{ctx}: node ids start at 1 (0 is the access point)")
    pos = rn.get("pos", [0.0, 0.0])
    if not isinstance(pos, (list, tuple)) or len(pos) != 2:
        raise ValueError(f"{ctx}.pos must be [x, y] in metres")
    return NodeConfig(node_id=nid, mac=normalize_mac(_require(rn, "mac", ctx)),
                      x=float(pos[0]), y=float(pos[1]), channel=int(rn.get("channel", 1)),
                      firmware=_parse_firmware(rn.get("firmware"), base_dir, ctx),
                      wifi=bool(rn.get("wifi", False)))


def _parse_traffic(raw: Any, mock_ids: set) -> list:
    if not isinstance(raw, list):
        raise ValueError("scenario.traffic must be a list")
    out = []
    for i, t in enumerate(raw):
        ctx = f"traffic[{i}]"
        t = _section(t, ctx, {"node", "at_us", "dst", "size_bytes", "count", "period_us"})
        nid = int(_require(t, "node", ctx))
        if nid not in mock_ids:
            raise ValueError(f"{ctx}: node {nid} is not a declared mock node "
                             "(firmware nodes generate their own traffic)")
        dst = t.get("dst", "broadcast")
        out.append(TrafficConfig(
            node_id=nid, at_us=int(_require(t, "at_us", ctx)),
            dst=BROADCAST_MAC if dst == "broadcast" else normalize_mac(dst),
            size_bytes=int(t.get("size_bytes", 16)),
            count=_positive(t.get("count", 1), f"{ctx}.count"),
            period_us=int(t.get("period_us", 0))))
    return out


def _parse_links(raw: Any, valid_ids: set) -> list:
    if not isinstance(raw, list):
        raise ValueError("scenario.links must be a list")
    out = []
    for i, rl in enumerate(raw):
        ctx = f"links[{i}]"
        rl = _section(rl, ctx, {"a", "b", "rssi_dbm", "loss"})
        a, b = int(_require(rl, "a", ctx)), int(_require(rl, "b", ctx))
        for nid in (a, b):
            if nid not in valid_ids:
                raise ValueError(f"{ctx}: node {nid} is not declared (0 is the access point)")
        if a == b:
            raise ValueError(f"{ctx}: a node cannot link to itself")
        if "rssi_dbm" not in rl and "loss" not in rl:
            raise ValueError(f"{ctx}: give rssi_dbm or loss")
        loss = float(rl.get("loss", 0.0))
        if not 0.0 <= loss <= 1.0:
            raise ValueError(f"{ctx}: loss must be in [0, 1], got {loss}")
        rssi = rl.get("rssi_dbm")
        out.append(Link(a, b, None if rssi is None else float(rssi), loss))
    return out


def _parse_ap(raw: Any, nodes: list) -> Optional[AccessPoint]:
    wifi_nodes = [n for n in nodes if n.wifi]
    if raw is None:
        if wifi_nodes:
            raise ValueError("nodes with wifi: true need a wifi_ap section")
        return None
    raw = _section(raw, "wifi_ap", {"pos", "channel", "mac", "ssid"})
    pos = _require(raw, "pos", "wifi_ap")
    ap = AccessPoint(mac=normalize_mac(raw.get("mac", "02:00:00:00:00:01")),
                     x=float(pos[0]), y=float(pos[1]), channel=int(raw.get("channel", 1)),
                     ssid=str(raw.get("ssid", "esp-netsim")))
    if not 1 <= len(ap.ssid.encode()) <= 32:
        raise ValueError("wifi_ap.ssid must be 1 to 32 bytes")
    for n in wifi_nodes:
        if n.firmware is None:
            raise ValueError(f"node {n.node_id}: wifi needs firmware (mock nodes have no IP stack)")
        if n.channel != ap.channel:
            raise ValueError(f"node {n.node_id}: a station must use the AP's channel {ap.channel}")
    return ap


def _parse_firmware(raw: Optional[str], base_dir: str, ctx: str) -> Optional[str]:
    if raw is None:
        return None
    path = os.path.normpath(os.path.join(base_dir, os.path.expanduser(raw)))
    args = os.path.join(path, "flasher_args.json")
    if not os.path.isfile(args):
        raise ValueError(f"{ctx}.firmware: {path} is not an ESP-IDF build dir "
                         "(no flasher_args.json; run idf.py build)")
    with open(args, encoding="utf-8") as f:
        chip = json.load(f)["extra_esptool_args"]["chip"]
    if chip != "esp32s3":
        raise ValueError(f"{ctx}.firmware: {path} targets {chip}; run idf.py set-target esp32s3")
    if os.name == "nt":
        raise ValueError(f"{ctx}.firmware: QEMU nodes need Linux or WSL (QMP over UNIX sockets)")
    return path


def _parse_medium(raw: Any, seed: int) -> MediumConfig:
    m = _section(raw, "medium", {"error_model", "packet_error_rate", "path_loss_exponent",
                                 "tx_power_dbm", "noise_figure_db", "max_retries"})
    defaults = MediumConfig()
    error_model = m.get("error_model", "snr_per")
    if error_model == "constant":
        model = ConstantErrorModel(float(m.get("packet_error_rate", 0.0)))
    elif error_model == "snr_per":
        model = SnrPerErrorModel()
    else:
        raise ValueError(f"medium.error_model must be constant|snr_per, got {error_model!r}")
    return MediumConfig(
        propagation=LogDistance(
            float(m.get("path_loss_exponent", defaults.propagation.exponent)),
            float(m.get("tx_power_dbm", defaults.propagation.tx_power_dbm))),
        error_model=model,
        noise_figure_db=float(m.get("noise_figure_db", defaults.noise_figure_db)),
        max_retries=int(m.get("max_retries", defaults.max_retries)),
        seed=seed)


def load_scenario(path: str) -> ScenarioConfig:
    with open(path, encoding="utf-8") as f:
        return parse_scenario(yaml.safe_load(f), os.path.dirname(os.path.abspath(path)))
