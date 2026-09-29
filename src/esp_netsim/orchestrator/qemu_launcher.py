"""Boot firmware nodes under the Espressif QEMU fork, each with its own identity, gated over QMP."""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config.schema import NodeConfig

FLASH_SIZE = 4 * 1024 * 1024
PARTITION_ENTRY = struct.Struct("<2sBBII16sI")
# filter-redirector chardevs: guest->medium, medium->slirp, slirp->medium, medium->guest.
WIFI_SOCKETS = ("up_out", "up_in", "dn_out", "dn_in")
NVS_TYPE, NVS_SUBTYPE = 1, 2


class QmpClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._r = reader
        self._w = writer

    # QMP over TCP stalls ~88 ms per command on delayed ACK; a UNIX socket takes ~0.75 ms (V-007).
    @classmethod
    async def connect_unix(cls, path: str) -> "QmpClient":
        reader, writer = await asyncio.open_unix_connection(path)
        client = cls(reader, writer)
        await client._readline_json()  # greeting
        await client._execute("qmp_capabilities")
        return client

    async def _readline_json(self) -> dict:
        line = await self._r.readline()
        return json.loads(line.decode("utf-8")) if line else {}

    async def _execute(self, command: str) -> dict:
        self._w.write((json.dumps({"execute": command}) + "\r\n").encode("utf-8"))
        await self._w.drain()
        while True:
            resp = await self._readline_json()
            if not resp or "return" in resp or "error" in resp:
                return resp

    async def cont(self) -> dict:
        return await self._execute("cont")

    async def stop(self) -> dict:
        return await self._execute("stop")


@dataclass
class QemuNode:
    node_id: int
    proc: asyncio.subprocess.Process
    qmp: QmpClient
    uart_log: Path
    wifi_sockets: dict   # WIFI_SOCKETS name -> path; empty unless the node is a Wi-Fi station


def find_qemu() -> str:
    """Locate qemu-system-xtensa: $ESP_NETSIM_QEMU, PATH, then the ESP-IDF tools dir."""
    found = (os.environ.get("ESP_NETSIM_QEMU") or shutil.which("qemu-system-xtensa")
             or next(iter(glob.glob(os.path.expanduser(
                 "~/.espressif/tools/qemu-xtensa/*/qemu/bin/qemu-system-xtensa"))), None))
    if not found:
        raise FileNotFoundError("qemu-system-xtensa not found; see firmware/qemu/setup_esp_idf.sh")
    return found


def nvs_identity(node: NodeConfig, port: int, size: int) -> bytes:
    """Build an NVS partition carrying the node's identity for the espnet shim to read."""
    csv = ("key,type,encoding,value\nespnet,namespace,,\n"
           f"node_id,data,u16,{node.node_id}\n"
           f"mac,data,hex2bin,{node.mac.replace(':', '')}\n"
           f"channel,data,u8,{node.channel}\n"
           f"port,data,u16,{port}\n")
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp, "nvs.csv"), Path(tmp, "nvs.bin")
        src.write_text(csv)
        subprocess.run([sys.executable, "-m", "esp_idf_nvs_partition_gen", "generate",
                        str(src), str(out), hex(size)], check=True, capture_output=True)
        return out.read_bytes()


def flash_image(node: NodeConfig, port: int) -> bytes:
    """Merge the node's ESP-IDF build into a 4 MB flash image with its identity in NVS."""
    build = Path(node.firmware)
    args = json.loads((build / "flasher_args.json").read_text())
    image = bytearray(b"\xff" * FLASH_SIZE)
    for offset, rel in args["flash_files"].items():
        blob = (build / rel).read_bytes()
        image[int(offset, 16):int(offset, 16) + len(blob)] = blob
    table = int(args["partition-table"]["offset"], 16)
    for i in range(table, table + 0x1000, PARTITION_ENTRY.size):
        magic, ptype, subtype, offset, size, _label, _flags = PARTITION_ENTRY.unpack_from(image, i)
        if magic == b"\xaa\x50" and (ptype, subtype) == (NVS_TYPE, NVS_SUBTYPE):
            image[offset:offset + size] = nvs_identity(node, port, size)
            return bytes(image)
    raise ValueError(f"{build}: partition table has no NVS partition")


async def launch(node: NodeConfig, port: int, workdir: Path) -> QemuNode:
    """Boot a node paused (-S) with its UART captured in workdir, and connect QMP."""
    # The guest blocks on every UART write, so the log must live on a fast local filesystem.
    flash, qmp_sock, log_path = (workdir / f"node{node.node_id}.{ext}"
                                 for ext in ("bin", "qmp", "log"))
    flash.write_bytes(flash_image(node, port))
    sockets = {k: workdir / f"node{node.node_id}.{k}" for k in WIFI_SOCKETS} if node.wifi else {}
    proc = await asyncio.create_subprocess_exec(
        find_qemu(), "-machine", "esp32s3", "-no-reboot", "-S", "-display", "none",
        "-monitor", "none", "-serial", f"file:{log_path}", *_nic_args(sockets),
        "-drive", f"file={flash},if=mtd,format=raw",
        "-qmp", f"unix:{qmp_sock},server=on,wait=off",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    for _ in range(200):
        try:
            return QemuNode(node.node_id, proc, await QmpClient.connect_unix(str(qmp_sock)),
                            log_path, sockets)
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    proc.kill()
    raise RuntimeError(f"node {node.node_id}: QEMU did not open QMP")


def _nic_args(wifi_sockets: dict) -> list[str]:
    """open_eth behind slirp NAT; for a Wi-Fi station every frame detours through the medium."""
    args = ["-netdev", "user,id=n0", "-net", "nic,netdev=n0,model=open_eth"]
    for name, path in wifi_sockets.items():
        args += ["-chardev", f"socket,id={name},path={path},server=on,wait=off"]
    if wifi_sockets:
        # queue=rx holds frames the guest sends toward slirp; queue=tx, frames slirp sends back.
        args += ["-object", "filter-redirector,id=up,netdev=n0,queue=rx,outdev=up_out,indev=up_in",
                 "-object", "filter-redirector,id=dn,netdev=n0,queue=tx,outdev=dn_out,indev=dn_in"]
    return args


async def terminate(nodes: list[QemuNode]) -> None:
    """Kill every QEMU process and reap it."""
    for n in nodes:
        if n.proc.returncode is None:
            n.proc.kill()
    await asyncio.gather(*(n.proc.wait() for n in nodes))
