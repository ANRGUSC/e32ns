"""esp-netsim edit: a local page to place nodes, see the links the medium predicts, and save."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations, takewhile
from pathlib import Path

import yaml

from .config.schema import parse_scenario

PAGE = Path(__file__).with_name("editor.html")
ESPNOW_PROBE = (50, 1.0)   # bytes, Mbps: a typical ESP-NOW payload at the ESP-NOW rate
WIFI_PROBE = (100, 11.0)   # station <-> AP at the fixed Wi-Fi rate


def predicted_links(doc: dict, base_dir: str) -> list[dict]:
    """Mean RSSI and one-attempt delivery for every pair that can hear the other."""
    sc = parse_scenario(doc, base_dir)
    engine = sc.build_engine()
    pairs = [(a.node_id, b.node_id, ESPNOW_PROBE, False)
             for a, b in combinations(sc.nodes, 2) if a.channel == b.channel]
    if sc.ap:
        pairs += [(n.node_id, sc.ap.node_id, WIFI_PROBE, True) for n in sc.nodes if n.wifi]
    links = []
    for a, b, (nbytes, rate), wifi in pairs:
        rssi, per = engine.link_quality(a, b, nbytes, rate)
        if per < 0.995:
            links.append({"a": a, "b": b, "wifi": wifi,
                          "rssi_dbm": round(rssi, 1), "pdr": round(1.0 - per, 3)})
    return links


class _Dumper(yaml.SafeDumper):
    """Block mappings, but [x, y] for lists of scalars such as pos."""


_Dumper.add_representer(list, lambda d, v: d.represent_sequence(
    "tag:yaml.org,2002:seq", v, flow_style=not any(isinstance(x, (dict, list)) for x in v)))


def dump_scenario(doc: dict, header: str) -> str:
    """Scenario YAML under the original leading comments, one flow-style line per node."""
    def block(d: dict) -> str:
        return yaml.dump(d, Dumper=_Dumper, sort_keys=False, default_flow_style=False) if d else ""
    nodes = "".join(f"  - {yaml.safe_dump(n, sort_keys=False, default_flow_style=True, width=200)}"
                    for n in doc["nodes"])
    rest = {k: v for k, v in doc.items() if k not in ("name", "nodes")}
    return (header + block({"name": doc.get("name", "scenario")}) + "\nnodes:\n" + nodes
            + ("\n" + block(rest) if rest else ""))


def serve(path: Path, port: int) -> ThreadingHTTPServer:
    """Build (not start) a localhost server editing the scenario file at path."""
    base_dir = str(path.parent)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/":
                self.send_error(404)
                return
            data = {"path": str(path), "doc": yaml.safe_load(path.read_text(encoding="utf-8"))}
            # "</" inside the JSON would end the <script> element early.
            page = PAGE.read_text(encoding="utf-8").replace(
                "/*DATA*/null", json.dumps(data).replace("</", "<\\/"))
            self._reply(200, "text/html; charset=utf-8", page)

        def do_POST(self):
            if self.path not in ("/links", "/save"):
                self.send_error(404)
                return
            try:
                doc = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                links = predicted_links(doc, base_dir)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                self._reply(400, "application/json", json.dumps({"error": str(exc)}))
                return
            if self.path == "/save":
                with path.open(encoding="utf-8") as f:
                    header = "".join(takewhile(lambda line: line.startswith("#"), f))
                path.write_text(dump_scenario(doc, header), encoding="utf-8")
            self._reply(200, "application/json", json.dumps({"links": links}))

        def _reply(self, code: int, ctype: str, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *args):
            pass  # one line per request would bury the URL the user needs

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
