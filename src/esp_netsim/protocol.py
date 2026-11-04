"""Node <-> medium wire protocol: length-prefixed big-endian binary messages (D-002)."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any, Optional

LENGTH_PREFIX = struct.Struct(">I")
MAX_MESSAGE_BYTES = 8 * 1024 * 1024  # a corrupt length prefix must not allocate gigabytes
BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"
STATUS_SUCCESS, STATUS_FAIL = "success", "fail"

HELLO, WELCOME, ADD_PEER, DEL_PEER, TX = "HELLO", "WELCOME", "ADD_PEER", "DEL_PEER", "TX"
TX_STATUS, RX, RUN_SLICE, SLICE_DONE, SHUTDOWN = "TX_STATUS", "RX", "RUN_SLICE", "SLICE_DONE", "SHUTDOWN"

# Per message: wire tag, fixed fields, and their names. MACs are 6 raw bytes; "data" is a uint16
# length whose bytes follow the fixed fields. espnet_shim.c mirrors these as packed structs.
_LAYOUT = {
    HELLO: (1, ">H6s", ("node", "mac")),
    WELCOME: (2, ">H", ("node",)),
    ADD_PEER: (3, ">H6sB16s", ("node", "peer", "encrypt", "lmk")),
    DEL_PEER: (4, ">H6s", ("node", "peer")),
    TX: (5, ">HQ6s6sBIH", ("node", "t", "src", "dst", "channel", "seq", "data")),
    TX_STATUS: (6, ">HIB", ("node", "seq", "status")),
    RX: (7, ">HQ6s6sBfH", ("node", "t", "src", "dst", "channel", "rssi_dbm", "data")),
    RUN_SLICE: (8, ">Q", ("until_t",)),
    SLICE_DONE: (9, ">HQ", ("node", "t")),
    SHUTDOWN: (10, ">", ()),
}
_BY_TAG = {tag: (name, struct.Struct(fmt), fields) for name, (tag, fmt, fields) in _LAYOUT.items()}
_MACS = ("mac", "peer", "src", "dst")
_MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")


def normalize_mac(mac: str) -> str:
    """Lowercase aa:bb:cc:dd:ee:ff; raises ValueError on anything else (config boundary)."""
    s = str(mac).strip().lower()
    if not _MAC_RE.fullmatch(s):
        raise ValueError(f"bad MAC {mac!r}: expected aa:bb:cc:dd:ee:ff")
    return s


def mac_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


@dataclass(frozen=True)
class Message:
    """A protocol message: a type plus its fields."""

    type: str
    payload: dict

    def get(self, key: str) -> Any:
        return self.payload[key]

    @property
    def node(self) -> Optional[int]:
        return self.payload.get("node")


def _to_wire(key: str, value: Any) -> Any:
    if key in _MACS:
        return mac_bytes(value)
    if key == "data":
        return len(value)
    if key == "status":
        return value == STATUS_SUCCESS
    return value


def _from_wire(key: str, value: Any) -> Any:
    if key in _MACS:
        return ":".join(f"{b:02x}" for b in value)
    if key == "status":
        return STATUS_SUCCESS if value else STATUS_FAIL
    if key == "encrypt":
        return bool(value)
    return value


def encode_body(msg: Message) -> bytes:
    tag, fmt, fields = _LAYOUT[msg.type]
    p = msg.payload
    return (bytes((tag,)) + struct.pack(fmt, *(_to_wire(k, p[k]) for k in fields))
            + p.get("data", b""))


def decode_body(raw: bytes) -> Message:
    if not raw or raw[0] not in _BY_TAG:
        raise ValueError(f"unknown message tag {raw[:1].hex() or 'none'}")
    name, fixed, fields = _BY_TAG[raw[0]]
    p = {k: _from_wire(k, v) for k, v in zip(fields, fixed.unpack_from(raw, 1))}
    if "data" in p:
        p["data"] = raw[1 + fixed.size:1 + fixed.size + p["data"]]
    return Message(name, p)


def encode_frame(msg: Message) -> bytes:
    """Length-prefix a message body for the wire."""
    body = encode_body(msg)
    return LENGTH_PREFIX.pack(len(body)) + body


class FrameDecoder:
    """Reassembles length-prefixed messages from arbitrarily chunked reads."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Message]:
        self._buf.extend(chunk)
        out = []
        while len(self._buf) >= LENGTH_PREFIX.size:
            (length,) = LENGTH_PREFIX.unpack_from(self._buf)
            if length > MAX_MESSAGE_BYTES:
                raise ValueError(f"declared frame length {length} exceeds cap")
            end = LENGTH_PREFIX.size + length
            if len(self._buf) < end:
                break
            out.append(decode_body(bytes(self._buf[LENGTH_PREFIX.size:end])))
            del self._buf[:end]
        return out
