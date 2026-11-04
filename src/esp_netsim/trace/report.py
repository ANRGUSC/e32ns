"""The replay page: written into each run's output, or served live while a run is in progress."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

TEMPLATE = Path(__file__).with_name("report.html")


def _page(data: object) -> str:
    # "</" inside the JSON would end the <script> element early.
    return TEMPLATE.read_text(encoding="utf-8").replace(
        "/*DATA*/null", json.dumps(data).replace("</", "<\\/"))


def read_logs(paths: dict[int, Path]) -> dict[int, str]:
    """Each node's UART log so far, by node id."""
    return {nid: p.read_text(errors="replace") for nid, p in paths.items() if p.exists()}


def write_report(out_dir: Path, results: dict) -> dict:
    """Embed the run's results and every node's UART log into the replay page; return the embedded run."""
    run = {**results, "logs": read_logs({n["id"]: out_dir / f"node{n['id']}.log"
                                         for n in results["nodes"]})}
    (out_dir / "report.html").write_text(_page(run), encoding="utf-8")
    return run


def _since(run: dict, query: str) -> dict:
    """The run without the events and console text the page already has (?events=N&logs=id:chars,...)."""
    q = parse_qs(query)
    have = dict(p.split(":") for p in q.get("logs", [""])[0].split(",") if p)
    return {**run, "events": run["events"][int(q.get("events", ["0"])[0]):],
            "logs": {nid: text[int(have.get(str(nid), 0)):] for nid, text in run["logs"].items()}}


class LiveView:
    """Serves the replay page on localhost, and as run.json what `state` returns that the page lacks."""

    def __init__(self, port: int):
        self.state: Callable[[], Optional[dict]] = lambda: None
        view = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                if url.path == "/":
                    self._reply("text/html; charset=utf-8", _page("live"))
                elif url.path != "/run.json":
                    self.send_error(404)
                elif (state := view.state()) is None:
                    self.send_error(503, "run not started yet")
                else:
                    self._reply("application/json", json.dumps(_since(state, url.query)))

            def _reply(self, ctype: str, body: str) -> None:
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt, *args):
                pass  # one line per poll would bury the run's own output

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/"
