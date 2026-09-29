#!/usr/bin/env python3
"""Measure QMP stop/cont barrier overhead on one real node (barrier spike, D-023 throughput axis).

The barrier gates every node with a QMP stop/cont each slice. This measures the fundamental
per-cycle cost so we know whether a given virtual slice can be held without collapsing
throughput. Reports, for each target virtual slice:
  * per-cycle wall time and the QMP round-trip overhead (wall not spent running the guest),
  * the real-time factor (wall / virtual advanced),
  * the sustained barrier-cycle rate (an upper bound on slices, hence frames, per second).

Usage: barrier_overhead_probe.py <flash.bin>
"""
import json
import os
import socket
import subprocess
import sys
import time

QMP_SOCK = "/tmp/espnet-ov-qmp.sock"

def find_qemu():
    base = os.path.expanduser("~/.espressif/tools/qemu-xtensa")
    for root, _d, files in os.walk(base):
        if "qemu-system-xtensa" in files:
            return os.path.join(root, "qemu-system-xtensa")
    raise SystemExit("qemu not found")

def connect(path):
    # QMP over a UNIX socket: no Nagle / delayed-ACK (the TCP variant measured ~88 ms/
    # cycle, a TCP artifact — D-024). This is also what qemu_launcher.py uses.
    for _ in range(200):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            return s
        except OSError:
            time.sleep(0.05)
    raise SystemExit("connect fail")

class Qmp:
    def __init__(self, sock):
        sock.settimeout(None)
        self.f = sock.makefile("rwb")
    def _recv(self):
        return json.loads(self.f.readline())
    def cmd(self, c, **a):
        m = {"execute": c}
        if a:
            m["arguments"] = a
        self.f.write((json.dumps(m) + "\r\n").encode()); self.f.flush()
        while True:
            r = self._recv()
            if "return" in r or "error" in r:
                return r
    def start(self):
        self._recv(); self.cmd("qmp_capabilities")

def run_cycles(qmp, n, slice_wall_s):
    """Do n barrier cycles: cont, (advance slice_wall_s), stop. Return total wall."""
    t0 = time.perf_counter()
    for _ in range(n):
        qmp.cmd("cont")
        if slice_wall_s > 0:
            time.sleep(slice_wall_s)
        qmp.cmd("stop")
    return time.perf_counter() - t0

def main():
    flash = sys.argv[1]
    qemu = find_qemu()
    if os.path.exists(QMP_SOCK):
        os.unlink(QMP_SOCK)
    proc = subprocess.Popen(
        [qemu, "-nographic", "-machine", "esp32s3",
         "-drive", f"file={flash},if=mtd,format=raw", "-no-reboot",
         "-qmp", f"unix:{QMP_SOCK},server=on,wait=off", "-S"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        qmp = Qmp(connect(QMP_SOCK))
        qmp.start()
        qmp.cmd("cont"); time.sleep(2.0); qmp.cmd("stop")   # let it boot, then pause

        N = 200
        # (a) pure QMP round-trip floor: cont+stop with ~no dwell.
        wall = run_cycles(qmp, N, 0.0)
        floor_us = wall / N * 1e6
        print(f"QMP stop/cont round-trip floor: {floor_us:.0f} us/cycle "
              f"({N/wall:.0f} cycles/s)")

        # (b) target virtual slices: dwell = slice, measure realtime factor.
        for slice_us in (1000, 250):
            dwell = slice_us / 1e6
            wall = run_cycles(qmp, N, dwell)
            per_cycle_us = wall / N * 1e6
            overhead_us = per_cycle_us - slice_us
            rt_factor = per_cycle_us / slice_us
            cyc_per_s = N / wall
            print(f"slice={slice_us:4d}us: {per_cycle_us:6.0f} us/cycle wall, "
                  f"overhead {overhead_us:5.0f} us, realtime x{rt_factor:.1f} slower, "
                  f"{cyc_per_s:.0f} slices/s")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

if __name__ == "__main__":
    main()
