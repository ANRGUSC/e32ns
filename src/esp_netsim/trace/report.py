"""Write report.html: a self-contained replay of a finished run (nodes, links, packets, consoles)."""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("report.html")


def write_report(out_dir: Path) -> Path:
    """Embed results.json and every node's UART log into the replay page."""
    run = json.loads((out_dir / "results.json").read_text())
    run["logs"] = {n["id"]: (out_dir / f"node{n['id']}.log").read_text(errors="replace")
                   for n in run["nodes"] if (out_dir / f"node{n['id']}.log").exists()}
    # "</" inside the JSON would end the <script> element early.
    data = json.dumps(run).replace("</", "<\\/")
    page = out_dir / "report.html"
    page.write_text(TEMPLATE.read_text(encoding="utf-8").replace("/*DATA*/null", data),
                    encoding="utf-8")
    return page
