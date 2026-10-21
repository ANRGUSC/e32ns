"""Node <-> Medium wire protocol codec: length-prefixed binary messages (D-002)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Optional

LENGTH_PREFIX = struct.Struct(">I")
MAX_MESSAGE_BYTES = 8 * 1024 * 1024  # cap guards against a corrupt length prefix
BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"

HELLO = "HELLO"
WELCOME = "WELCOME"
ADD_PEER = "ADD_PEER"
DEL_PEER = "DEL_PEER"
TX = "TX"
TX_STATUS = "TX_STATUS"
RX = "RX"
RUN_SLICE = "RUN_SLICE"
SLICE_DONE = "SLICE_DONE"
SHUTDOWN = "SHUTDOWN"

STATUS_SUCCESS = "success"
STATUS_FAIL = "fail"

# Binary wire-body tags (must match firmware/components/espnet_shim).
_TAG = {HELLO: 1, WELCOME: 2, ADD_PEER: 3, DEL_PEER: 4, TX: 5, TX_STATUS: 6,
        RX: 7, RUN_SLICE: 8, SLICE_DONE: 9, SHUTDOWN: 10}
_TAG_INV = {v: k for k, v in _TAG.items()}


def normalize_mac(mac: str) -> str:
    """Canonical lowercase ``aa:bb:cc:dd:ee:ff``; raises on bad input (wire/config boundary)."""
    s = mac.strip().lower().replace("-", ":")
    if ":" not in s:  # bare hex form
        if len(s) != 12:
            raise ValueError(f"bad MAC (expected 12 hex digits): {mac!r}")
        s = ":".join(s[i : i + 2] for i in range(0, 12, 2))
    parts = s.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise ValueError(f"bad MAC format: {mac!r}")
    try:
        int(s.replace(":", ""), 16)
    except ValueError as exc:  # non-hex digits
        raise ValueError(f"bad MAC (non-hex): {mac!r}") from exc
    return s


def is_broadcast(mac: str) -> bool:
    return normalize_mac(mac) == BROADCAST_MAC


@dataclass(frozen=True)
class Message:
    """A protocol message: a type tag plus a payload dict."""

    type: str
    payload: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    @property
    def node(self) -> Optional[int]:
        return self.payload.get("node")



def hello(node: int, mac: str) -> Message:
    return Message(HELLO, {"node": node, "mac": normalize_mac(mac),
                           "caps": {"espnow": True, "wifi": False}})


def welcome(node: int, slice_us: int, mac: str) -> Message:
    return Message(WELCOME, {"node": node, "slice_us": slice_us, "mac": normalize_mac(mac)})


def add_peer(node: int, peer: str, channel: int, encrypt: bool = False,
             lmk: Optional[bytes] = None) -> Message:
    return Message(ADD_PEER, {"node": node, "peer": normalize_mac(peer),
                              "channel": channel, "encrypt": encrypt,
                              "lmk": lmk})


def tx(node: int, t: int, src: str, dst: str, channel: int, rate_mbps: float,
       seq: int, data: bytes) -> Message:
    return Message(TX, {"node": node, "t": t, "src": normalize_mac(src),
                        "dst": normalize_mac(dst), "channel": channel,
                        "rate_mbps": rate_mbps, "seq": seq, "data": data})


def tx_status(node: int, seq: int, status: str, t: int) -> Message:
    return Message(TX_STATUS, {"node": node, "seq": seq, "status": status, "t": t})


def rx(node: int, t: int, src: str, dst: str, channel: int, rate_mbps: float,
       rssi_dbm: float, data: bytes) -> Message:
    return Message(RX, {"node": node, "t": t, "src": normalize_mac(src),
                        "dst": normalize_mac(dst), "channel": channel,
                        "rate_mbps": rate_mbps, "rssi_dbm": rssi_dbm,
                        "data": data})


def run_slice(until_t: int) -> Message:
    return Message(RUN_SLICE, {"until_t": until_t})


def slice_done(node: int, t: int) -> Message:
    return Message(SLICE_DONE, {"node": node, "t": t})


def shutdown() -> Message:
    return Message(SHUTDOWN, {})


# Binary body: uint8 tag, then fixed big-endian fields; MACs are 6 raw bytes,
# payloads are (uint16 length + bytes). Must stay in sync with the C shim.

def _mac_bytes(mac: str) -> bytes:
    return bytes.fromhex(normalize_mac(mac).replace(":", ""))


def _mac_str(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def encode_body(msg: Message) -> bytes:
    t, p = msg.type, msg.payload
    out = bytearray([_TAG[t]])
    if t == HELLO:
        caps = p.get("caps", {})
        capbits = (1 if caps.get("espnow") else 0) | (2 if caps.get("wifi") else 0)
        out += struct.pack(">H", p["node"]) + _mac_bytes(p["mac"]) + bytes([capbits])
    elif t == WELCOME:
        out += struct.pack(">HI", p["node"], p["slice_us"]) + _mac_bytes(p["mac"])
    elif t == ADD_PEER:
        lmk = ((p.get("lmk") or b"") + bytes(16))[:16]
        out += (struct.pack(">H", p["node"]) + _mac_bytes(p["peer"])
                + bytes([p.get("channel", 0) & 0xFF, 1 if p.get("encrypt") else 0]) + lmk)
    elif t == DEL_PEER:
        out += struct.pack(">H", p["node"]) + _mac_bytes(p["peer"])
    elif t == TX:
        data = p["data"]
        out += (struct.pack(">HQ", p["node"], p.get("t", 0))
                + _mac_bytes(p["src"]) + _mac_bytes(p["dst"])
                + bytes([p["channel"] & 0xFF]) + struct.pack(">f", p["rate_mbps"])
                + struct.pack(">I", p["seq"]) + struct.pack(">H", len(data)) + data)
    elif t == TX_STATUS:
        st = 1 if p["status"] == STATUS_SUCCESS else 0
        out += struct.pack(">HI", p["node"], p["seq"]) + bytes([st]) + struct.pack(">Q", p["t"])
    elif t == RX:
        data = p["data"]
        out += (struct.pack(">HQ", p["node"], p.get("t", 0))
                + _mac_bytes(p["src"]) + _mac_bytes(p["dst"])
                + bytes([p["channel"] & 0xFF])
                + struct.pack(">ff", p["rate_mbps"], p["rssi_dbm"])
                + struct.pack(">H", len(data)) + data)
    elif t == RUN_SLICE:
        out += struct.pack(">Q", p["until_t"])
    elif t == SLICE_DONE:
        out += struct.pack(">HQ", p["node"], p["t"])
    return bytes(out)


def decode_body(raw: bytes) -> Message:
    if not raw:
        raise ValueError("empty message body")
    tag = raw[0]
    t = _TAG_INV.get(tag)
    if t is None:
        raise ValueError(f"unknown message tag {tag}")
    o = 1  # cursor past the tag byte

    def u(fmt):  # unpack helper advancing the cursor
        nonlocal o
        vals = struct.unpack_from(fmt, raw, o)
        o += struct.calcsize(fmt)
        return vals

    def mac():
        nonlocal o
        m = _mac_str(raw[o:o + 6]); o += 6
        return m

    if t == HELLO:
        (node,) = u(">H"); m = mac(); (capbits,) = u(">B")
        return Message(t, {"node": node, "mac": m,
                           "caps": {"espnow": bool(capbits & 1), "wifi": bool(capbits & 2)}})
    if t == WELCOME:
        node, slice_us = u(">HI"); m = mac()
        return Message(t, {"node": node, "slice_us": slice_us, "mac": m})
    if t == ADD_PEER:
        (node,) = u(">H"); peer = mac(); channel, enc = u(">BB")
        lmk = raw[o:o + 16]; o += 16
        return Message(t, {"node": node, "peer": peer, "channel": channel,
                           "encrypt": bool(enc), "lmk": lmk if enc else None})
    if t == DEL_PEER:
        (node,) = u(">H"); peer = mac()
        return Message(t, {"node": node, "peer": peer})
    if t == TX:
        node, ts = u(">HQ"); src = mac(); dst = mac(); (channel,) = u(">B")
        (rate,) = u(">f"); (seq,) = u(">I"); (dlen,) = u(">H")
        data = raw[o:o + dlen]; o += dlen
        return Message(t, {"node": node, "t": ts, "src": src, "dst": dst,
                           "channel": channel, "rate_mbps": rate, "seq": seq,
                           "data": data})
    if t == TX_STATUS:
        node, seq = u(">HI"); (st,) = u(">B"); (ts,) = u(">Q")
        return Message(t, {"node": node, "seq": seq,
                           "status": STATUS_SUCCESS if st else STATUS_FAIL, "t": ts})
    if t == RX:
        node, ts = u(">HQ"); src = mac(); dst = mac(); (channel,) = u(">B")
        rate, rssi = u(">ff"); (dlen,) = u(">H")
        data = raw[o:o + dlen]; o += dlen
        return Message(t, {"node": node, "t": ts, "src": src, "dst": dst,
                           "channel": channel, "rate_mbps": rate, "rssi_dbm": rssi,
                           "data": data})
    if t == RUN_SLICE:
        (until_t,) = u(">Q")
        return Message(t, {"until_t": until_t})
    if t == SLICE_DONE:
        node, ts = u(">HQ")
        return Message(t, {"node": node, "t": ts})
    return Message(t, {})  # SHUTDOWN


def encode_frame(msg: Message) -> bytes:
    """Length-prefix a message body for the wire."""
    body = encode_body(msg)
    return LENGTH_PREFIX.pack(len(body)) + body


class FrameDecoder:
    """Reassembles length-prefixed messages from arbitrarily-chunked byte reads."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Message]:
        self._buf.extend(chunk)
        out: list[Message] = []
        while True:
            if len(self._buf) < LENGTH_PREFIX.size:
                break
            (length,) = LENGTH_PREFIX.unpack_from(self._buf, 0)
            if length > MAX_MESSAGE_BYTES:
                raise ValueError(f"declared frame length {length} exceeds cap")
            if len(self._buf) < LENGTH_PREFIX.size + length:
                break  # wait for more bytes
            start = LENGTH_PREFIX.size
            body = bytes(self._buf[start : start + length])
            del self._buf[: start + length]
            out.append(decode_body(body))
        return out
