#!/usr/bin/env python3
"""Verify the QMP clock-freeze assumption behind D-022 (barrier spike, step 1).

If the Espressif QEMU fork's guest clock genuinely freezes on QMP `stop`, then a paused
wall-clock interval must NOT appear in the guest's esp_timer. This is the load-bearing
assumption for using esp_timer as virtual time (D-022) and for the whole barrier design.

Method: boot a firmware that logs `gt=<esp_timer_us>` every 200 ms over serial (TCP). Let it
run, then QMP `stop` for a known wall-clock duration, `cont`, and keep logging. Compare, across
the pause, how much guest time advanced vs. wall-clock time. Freeze holds iff guest_delta is a
normal ~200 ms loop step while wall_delta is the full pause.

Usage: clock_freeze_probe.py <flash.bin> [pause_seconds]
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

PAUSE_S = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
FLASH = sys.argv[1]
SER_PORT, QMP_PORT = 5556, 5557

def find_qemu():
    base = os.path.expanduser("~/.espressif/tools/qemu-xtensa")
    for root, _d, files in os.walk(base):
        if "qemu-system-xtensa" in files:
            return os.path.join(root, "qemu-system-xtensa")
    raise SystemExit("qemu-system-xtensa not found")

def connect(port, tries=200):
    for _ in range(tries):
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1)
        except OSError:
            time.sleep(0.05)
    raise SystemExit(f"could not connect to {port}")

class Qmp:
    def __init__(self, sock):
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
        self._recv()               # greeting
        self.cmd("qmp_capabilities")

samples = []  # (host_wall, guest_us)
_run = True

def reader(sock):
    buf = b""
    pat = re.compile(rb"gt=(\d+)")
    sock.settimeout(None)  # blocking: a paused VM produces no data for seconds (not EOF)
    while _run:
        try:
            b = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            return
        if not b:
            return
        buf += b
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            m = pat.search(line)
            if m:
                samples.append((time.time(), int(m.group(1))))

def main():
    qemu = find_qemu()
    proc = subprocess.Popen(
        [qemu, "-nographic", "-machine", "esp32s3",
         "-drive", f"file={FLASH},if=mtd,format=raw", "-no-reboot",
         "-serial", f"tcp:127.0.0.1:{SER_PORT},server=on,wait=off",
         "-qmp", f"tcp:127.0.0.1:{QMP_PORT},server=on,wait=off", "-S"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ser = connect(SER_PORT)
        qmp = Qmp(connect(QMP_PORT))
        qmp.start()
        t = threading.Thread(target=reader, args=(ser,), daemon=True)
        t.start()
        print("cont     ->", qmp.cmd("cont"))
        time.sleep(4.0)                       # collect steady-state samples
        print("status(pre-stop) ->", qmp.cmd("query-status"))
        print("stop     ->", qmp.cmd("stop"))
        print("status(post-stop)->", qmp.cmd("query-status"))
        pause_wall_start = time.time()
        time.sleep(PAUSE_S)                    # hold the VM paused for a known wall interval
        print("cont     ->", qmp.cmd("cont"))
        pause_wall = time.time() - pause_wall_start
        time.sleep(3.0)                        # collect post-resume samples
    finally:
        global _run
        _run = False
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # Find the sample pair straddling the pause: the largest host-time gap.
    if len(samples) < 5:
        print(f"INCONCLUSIVE: only {len(samples)} samples"); return
    t0 = samples[0][0]
    g0 = samples[0][1]
    print("idx  host_s  guest_s")
    for k, (h, g) in enumerate(samples):
        print(f"{k:3d}  {h - t0:6.2f}  {(g - g0)/1e6:7.3f}")
    gaps = [(samples[i + 1][0] - samples[i][0], i) for i in range(len(samples) - 1)]
    top = sorted(gaps, reverse=True)[:5]
    print("top host gaps (s):", [f"{g:.2f}@{i}" for g, i in top])
    print(f"total samples: {len(samples)}")
    host_gap, i = max(gaps)
    guest_gap_us = samples[i + 1][1] - samples[i][1]
    print(f"paused for {pause_wall:.2f}s wall")
    print(f"straddling pair: host_gap={host_gap:.2f}s  guest_gap={guest_gap_us/1e6:.3f}s")
    # Freeze holds if the guest advanced far less than the wall pause.
    froze = guest_gap_us / 1e6 < (host_gap * 0.5)
    print(f"guest advanced {guest_gap_us/1e6:.3f}s while wall advanced {host_gap:.2f}s")
    print("RESULT:", "FREEZE_CONFIRMED (D-022 holds)" if froze
          else "FREEZE_VIOLATED (D-022 collapses)")

if __name__ == "__main__":
    main()
