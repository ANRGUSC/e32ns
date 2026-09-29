"""esp-netsim CLI: run | validate | info."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config.schema import load_scenario
from .orchestrator.runner import run_scenario_sync


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
    if args.trace:
        sc = sc.__class__(**{**sc.__dict__, "trace_path": args.trace})
    print(f"Running '{sc.name}': {len(sc.nodes)} nodes, {sc.duration_us} us "
          f"@ {sc.slice_us} us slices (seed {sc.seed})...")
    result = run_scenario_sync(sc, trace_override=args.trace)
    s = result.stats
    print("--- results ---")
    print(f"slices run     : {s.slices}")
    print(f"transmissions  : {s.tx_total}  (success {s.tx_success}, fail {s.tx_fail})")
    print(f"receptions     : delivered {s.rx_delivered}, dropped {s.rx_dropped}")
    if s.tx_total:
        pdr = s.tx_success / s.tx_total
        print(f"send-callback PDR: {pdr:.1%}")
    if result.trace_path:
        print(f"trace written  : {result.trace_path}")
    # Per-node received counts (mock-node inspection).
    for nid, node in sorted(result.nodes.items()):
        print(f"  node {nid} ({node.mac}): received {len(node.received)} frames")
    return 0


def _cmd_info(args) -> int:
    print(f"esp-netsim {__version__}")
    print("error models : constant | range (UDGM) | snr_per (YANS-style)")
    print("trace format : pcapng + radiotap (LINKTYPE 127), simulation-clock us")
    print("node kinds   : mock (built-in) | qemu (see docs/QEMU_INTEGRATION.md)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="esp-netsim",
                                description="Cooja-like network emulator for ESP32-S3.")
    p.add_argument("--version", action="version", version=f"esp-netsim {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run a scenario end-to-end (mock nodes)")
    pr.add_argument("scenario", help="path to scenario YAML/JSON")
    pr.add_argument("--trace", help="override trace output path (.pcapng)")
    pr.set_defaults(func=_cmd_run)

    pv = sub.add_parser("validate", help="validate a scenario file")
    pv.add_argument("scenario")
    pv.set_defaults(func=_cmd_validate)

    pi = sub.add_parser("info", help="print version and capabilities")
    pi.set_defaults(func=_cmd_info)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
