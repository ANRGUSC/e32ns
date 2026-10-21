"""esp-netsim CLI: run | edit | validate."""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path

from . import __version__, editor
from .config.schema import load_scenario
from .orchestrator.runner import run_scenario


def _cmd_validate(args) -> int:
    try:
        sc = load_scenario(args.scenario)
    except (ValueError, OSError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 2
    print(f"OK: scenario '{sc.name}' — {len(sc.nodes)} nodes, "
          f"error_model={sc.medium.error_model}, slice={sc.slice_us}us, "
          f"duration={sc.duration_us}us, seed={sc.seed}, "
          f"traffic_events={len(sc.traffic)}")
    return 0


def _cmd_run(args) -> int:
    sc = load_scenario(args.scenario)
    qemu = sum(1 for n in sc.nodes if n.firmware)
    print(f"Running '{sc.name}': {len(sc.nodes)} nodes ({qemu} QEMU, {len(sc.nodes) - qemu} mock), "
          f"{sc.duration_us} us @ {sc.slice_us} us slices (seed {sc.seed})...")
    result = asyncio.run(run_scenario(sc))
    s = result.stats
    print(f"slices run     : {s.slices} (effective quantum {sc.duration_us // max(s.slices, 1)} us "
          "of virtual time)")
    print(f"transmissions  : {s.tx_total}  (success {s.tx_success}, fail {s.tx_fail})")
    print(f"receptions     : delivered {s.rx_delivered}, dropped {s.rx_dropped}")
    for n in sc.nodes:
        sent = len({(e["src"], e["seq"]) for e in result.events if e["src"] == n.node_id})
        got = sum(1 for e in result.events if e["rx"] == n.node_id and e["delivered"])
        kind = "qemu" if n.firmware else "mock"
        print(f"  node {n.node_id} ({kind}, {n.mac}): sent {sent}, received {got}")
    print(f"results        : {result.out_dir}/ (trace.pcapng, results.json, node logs)")
    print(f"replay         : {result.out_dir / 'report.html'}")
    return 0


def _cmd_edit(args) -> int:
    server = editor.serve(Path(args.scenario).resolve(), args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Editing {args.scenario} at {url} (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="esp-netsim",
                                description="Cooja-like network emulator for ESP32-S3.")
    p.add_argument("--version", action="version", version=f"esp-netsim {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run a scenario end to end")
    pr.add_argument("scenario", help="path to scenario YAML")
    pr.set_defaults(func=_cmd_run)

    pv = sub.add_parser("validate", help="validate a scenario file")
    pv.add_argument("scenario")
    pv.set_defaults(func=_cmd_validate)

    pe = sub.add_parser("edit", help="place nodes on a map, see predicted links, save the scenario")
    pe.add_argument("scenario")
    pe.add_argument("--port", type=int, default=8765)
    pe.set_defaults(func=_cmd_edit)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
