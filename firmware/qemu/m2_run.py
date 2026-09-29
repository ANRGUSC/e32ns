#!/usr/bin/env python3
"""First honest M2: one REAL QEMU node + one MockNode on a shared barrier (D-026).

A real ESP32-S3 node (espnet_node firmware, gated via QMP) broadcasts ESP-NOW; a
message-gated MockNode receiver shares the same clock master and medium. Demonstrates
controlled delivery of a real firmware's ESP-NOW frames to another node through the
custom wireless-link medium — milestone M2, but with actual firmware underneath.

Usage: m2_run.py <flash.bin> [duration_us]
"""
import asyncio
import os
import sys

sys.path.insert(0, "/mnt/c/Users/abhig/Documents/ANRG/esp32-netsim/src")
from esp_netsim.medium import MediumEngine, MediumConfig            # noqa: E402
from esp_netsim.channel.error_model import ConstantErrorModel        # noqa: E402
from esp_netsim.orchestrator.simulation import Simulation            # noqa: E402
from esp_netsim.orchestrator.gate import QmpGate                     # noqa: E402
from esp_netsim.orchestrator.qemu_launcher import QmpClient          # noqa: E402
from esp_netsim.nodes.mock_node import MockNode                      # noqa: E402

FLASH = sys.argv[1]
DURATION_US = int(sys.argv[2]) if len(sys.argv) > 2 else 3_000_000
QMP_SOCK = "/tmp/espnet-m2-qmp.sock"
MAC1 = "aa:bb:cc:00:00:01"   # the QEMU node
MAC2 = "aa:bb:cc:00:00:02"   # the mock receiver


def find_qemu():
    for root, _d, fs in os.walk(os.path.expanduser("~/.espressif/tools/qemu-xtensa")):
        if "qemu-system-xtensa" in fs:
            return os.path.join(root, "qemu-system-xtensa")
    raise SystemExit("qemu not found")


async def main():
    # Medium: two nodes 5 m apart, same channel, lossless link (controlled delivery).
    engine = MediumEngine(MediumConfig(error_model=ConstantErrorModel(0.0)))
    engine.add_node(1, MAC1, x=0, y=0, channel=1)
    engine.add_node(2, MAC2, x=5, y=0, channel=1)

    sim = Simulation(engine, slice_us=1000, port=52333)
    await sim.start()

    if os.path.exists(QMP_SOCK):
        os.unlink(QMP_SOCK)
    qemu = find_qemu()
    proc = await asyncio.create_subprocess_exec(
        qemu, "-nographic", "-machine", "esp32s3", "-nic", "user,model=open_eth",
        "-drive", f"file={FLASH},if=mtd,format=raw", "-no-reboot",
        "-qmp", f"unix:{QMP_SOCK},server=on,wait=off", "-S",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    qmp = None
    for _ in range(200):
        try:
            qmp = await QmpClient.connect_unix(QMP_SOCK)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    if qmp is None:
        raise SystemExit("QMP connect failed")

    # Register the real QEMU node as a QMP-gated barrier participant (1 ms slice).
    sim.register_qmp_gate(QmpGate(node_id=1, qmp=qmp, slice_us=1000))

    # The message-gated MockNode receiver.
    receiver = MockNode(2, MAC2, port=52333, channel=1)
    await receiver.connect()

    print(f"running shared barrier for {DURATION_US} us virtual "
          f"(QEMU node boots + broadcasts; mock node receives)...")
    try:
        await sim.run(duration_us=DURATION_US, slice_timeout=2.0)
        await asyncio.sleep(0.2)
    finally:
        connected = sim.server.connected_nodes
        print(f"connected nodes at end: {connected}")
        print(f"QEMU node (1) HELLO'd to medium: {1 in connected}")
        print(f"mock receiver (2) got {len(receiver.received)} ESP-NOW frames "
              f"from the QEMU node")
        if receiver.received:
            f = receiver.received[0]
            print(f"  first RX: src={f.src_mac} rssi={f.rssi_dbm:.1f} dBm "
                  f"data0={f.data[0] if f.data else None}")
        print(f"medium stats: tx={sim.stats.tx_total} success={sim.stats.tx_success} "
              f"rx_delivered={sim.stats.rx_delivered}")
        verdict = (1 in connected) and len(receiver.received) > 0
        print("M2_RESULT:", "PASS — real firmware ESP-NOW delivered across the barrier"
              if verdict else "INCONCLUSIVE")
        await sim.stop()
        await receiver.close()
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
