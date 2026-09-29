#!/usr/bin/env python3
"""Latency-fidelity axis of the barrier spike (D-023, exercises D-014).

Runs a real QEMU node under the QMP `stop`/`cont` barrier while collecting each ESP-NOW
frame's real guest `esp_timer` timestamp (= virtual time, D-022) at the host medium. Then,
for each candidate slice S, computes the barrier's DISPATCH latency a receiver would observe:
the medium computes delivery at `t + airtime`, but under the barrier it can only dispatch at
the next slice boundary (D-014), so

    effective_latency = ceil((t + airtime)/S)*S - t

and the fidelity error vs the medium's computed value is (effective - airtime)/airtime.
D-023 passes if the MEDIAN error <= 20%. This is the quantization the reviewer flagged: the
~0.75 ms barrier overhead is the same order as the ~1 ms MAC latency, so a coarse slice may
fail. Uses REAL firmware timestamps, not assumed distributions.

Usage: barrier_latency_probe.py <flash.bin>
"""
import math
import os
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/mnt/c/Users/abhig/Documents/ANRG/esp32-netsim/src")
from esp_netsim.protocol import FrameDecoder, TX  # noqa: E402
from esp_netsim.channel.airtime import AirtimeModel  # noqa: E402

MEDIUM_PORT = 52333
QMP_SOCK = "/tmp/espnet-lat-qmp.sock"
TARGET_FRAMES = 300
SLICE_WALL_S = 0.001          # gate the node in ~1 ms virtual slices

frames = []                   # collected (t_tx_us, payload_len)
_lock = threading.Lock()
_stop = threading.Event()

def server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", MEDIUM_PORT)); srv.listen(1); srv.settimeout(1.0)
    conn = None
    while not _stop.is_set() and conn is None:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
    if conn is None:
        return
    conn.settimeout(1.0)
    dec = FrameDecoder()
    while not _stop.is_set():
        try:
            b = conn.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        if not b:
            break
        for m in dec.feed(b):
            if m.type == TX:
                with _lock:
                    frames.append((m.get("t"), len(bytes.fromhex(m.get("data") or ""))))

def find_qemu():
    for root, _d, fs in os.walk(os.path.expanduser("~/.espressif/tools/qemu-xtensa")):
        if "qemu-system-xtensa" in fs:
            return os.path.join(root, "qemu-system-xtensa")
    raise SystemExit("qemu not found")

def qmp_connect(path):
    for _ in range(200):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(path); return s
        except OSError:
            time.sleep(0.05)
    raise SystemExit("qmp connect fail")

def qmp_cmd(f, c):
    f.write((__import__("json").dumps({"execute": c}) + "\r\n").encode()); f.flush()
    import json
    while True:
        r = json.loads(f.readline())
        if "return" in r or "error" in r:
            return r

def analyze(slice_us):
    at = AirtimeModel()
    # deterministic airtime floor (no backoff) for a 16-byte acknowledged unicast
    with _lock:
        data = list(frames)
    effs, comps = [], []
    for t, dlen in data:
        airtime = at.tx_airtime_us(dlen, 1.0, acknowledged=True)  # computed latency
        delivery = t + airtime
        dispatch = math.ceil(delivery / slice_us) * slice_us
        effs.append(dispatch - t)
        comps.append(airtime)
    med_eff = statistics.median(effs)
    med_comp = statistics.median(comps)
    err = (med_eff - med_comp) / med_comp
    return med_comp, med_eff, err

def main():
    flash = sys.argv[1]
    th = threading.Thread(target=server, daemon=True); th.start()
    if os.path.exists(QMP_SOCK):
        os.unlink(QMP_SOCK)
    qemu = find_qemu()
    proc = subprocess.Popen(
        [qemu, "-nographic", "-machine", "esp32s3", "-nic", "user,model=open_eth",
         "-drive", f"file={flash},if=mtd,format=raw", "-no-reboot",
         "-qmp", f"unix:{QMP_SOCK},server=on,wait=off", "-S"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        qf = qmp_connect(QMP_SOCK).makefile("rwb")
        import json
        json.loads(qf.readline())            # greeting
        qmp_cmd(qf, "qmp_capabilities")
        # Barrier loop: gate the node in ~1 ms slices until frames arrive (or timeout).
        deadline = time.time() + 60
        while time.time() < deadline:
            qmp_cmd(qf, "cont")
            time.sleep(SLICE_WALL_S)
            qmp_cmd(qf, "stop")
            with _lock:
                n = len(frames)
            if n >= TARGET_FRAMES:
                break
    finally:
        _stop.set()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    with _lock:
        n = len(frames)
    print(f"collected {n} real ESP-NOW frames under the QMP barrier")
    if n < 20:
        print("INCONCLUSIVE: too few frames"); return
    ts = [t for t, _ in frames]
    print(f"guest t range: {min(ts)}..{max(ts)} us  (esp_timer = virtual time, D-022)")
    print("slice_us  computed_us  effective_us  median_error   verdict(<=20%)")
    for S in (1000, 250):
        comp, eff, err = analyze(S)
        print(f"{S:7d}   {comp:9.0f}    {eff:9.0f}     {err*100:6.1f}%      "
              f"{'PASS' if abs(err) <= 0.20 else 'FAIL'}")

if __name__ == "__main__":
    main()
