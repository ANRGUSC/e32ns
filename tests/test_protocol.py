"""Tests for the wire-protocol codec (protocol.py)."""

import pytest

from esp_netsim import protocol as p


def test_normalize_mac_forms():
    assert p.normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"
    assert p.normalize_mac("aa-bb-cc-dd-ee-ff") == "aa:bb:cc:dd:ee:ff"
    assert p.normalize_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize("bad", ["", "gg:bb:cc:dd:ee:ff", "aa:bb:cc", "aabbccddee"])
def test_normalize_mac_rejects_garbage(bad):
    with pytest.raises(ValueError):
        p.normalize_mac(bad)


def test_broadcast_detection():
    assert p.is_broadcast("FF:FF:FF:FF:FF:FF")
    assert not p.is_broadcast("aa:bb:cc:dd:ee:ff")


def test_roundtrip_tx_message():
    payload = bytes(range(0, 250))  # ESP-NOW v1 max payload (brief §3)
    msg = p.tx(node=1, t=1234, src="aa:bb:cc:00:00:01", dst=p.BROADCAST_MAC,
              channel=6, rate_mbps=1.0, seq=7, data=payload)
    frame = p.encode_frame(msg)
    dec = p.FrameDecoder()
    got = dec.feed(frame)
    assert len(got) == 1
    m = got[0]
    assert m.type == p.TX
    assert m.node == 1 and m.t == 1234 and m.get("seq") == 7
    assert p.hex_to_data(m.get("data")) == payload


def test_decoder_handles_split_and_coalesced_streams():
    msgs = [p.hello(1, "aa:bb:cc:dd:ee:01"),
            p.tx(1, 10, "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", 6, 1.0, 1, b"hi"),
            p.slice_done(1, 1000)]
    blob = b"".join(p.encode_frame(m) for m in msgs)

    # Feed one byte at a time: decoder must reassemble exactly the 3 messages in order.
    dec = p.FrameDecoder()
    out = []
    for i in range(len(blob)):
        out.extend(dec.feed(blob[i : i + 1]))
    assert [m.type for m in out] == [p.HELLO, p.TX, p.SLICE_DONE]
    assert dec.pending_bytes == 0

    # Feed everything in one chunk: same result.
    dec2 = p.FrameDecoder()
    out2 = dec2.feed(blob)
    assert [m.type for m in out2] == [p.HELLO, p.TX, p.SLICE_DONE]


def test_decoder_rejects_oversize_length_prefix():
    bogus = p.LENGTH_PREFIX.pack(p.MAX_MESSAGE_BYTES + 1)
    dec = p.FrameDecoder()
    with pytest.raises(ValueError):
        dec.feed(bogus)


def test_binary_wire_layout_is_locked():
    """Byte-level layout the C shim must match (keep C and Python in sync)."""
    import struct
    # TX: tag(5) | node(u16) | t(u64) | src(6) | dst(6) | ch(u8) | rate(f32) |
    #     seq(u32) | dlen(u16) | data
    m = p.tx(node=2, t=0x0102, src="aa:bb:cc:dd:ee:01", dst="ff:ff:ff:ff:ff:ff",
             channel=6, rate_mbps=1.0, seq=9, data=b"\x01\x02\x03")
    body = p.encode_body(m)
    assert body[0] == p._TAG[p.TX] == 5
    assert struct.unpack_from(">H", body, 1)[0] == 2         # node
    assert struct.unpack_from(">Q", body, 3)[0] == 0x0102    # t
    assert body[11:17] == bytes.fromhex("aabbccddee01")      # src mac
    assert body[17:23] == b"\xff" * 6                        # dst mac
    assert body[23] == 6                                     # channel
    assert struct.unpack_from(">f", body, 24)[0] == 1.0      # rate
    assert struct.unpack_from(">I", body, 28)[0] == 9        # seq
    assert struct.unpack_from(">H", body, 32)[0] == 3        # data len
    assert body[34:37] == b"\x01\x02\x03"                    # data


def test_hello_and_slice_done_layout():
    import struct
    h = p.hello(1, "aa:bb:cc:00:00:01", espnow=True, wifi=False)
    hb = p.encode_body(h)
    assert hb[0] == 1 and struct.unpack_from(">H", hb, 1)[0] == 1
    assert hb[3:9] == bytes.fromhex("aabbcc000001")
    assert hb[9] == 0b01  # espnow set, wifi clear
    sd = p.encode_body(p.slice_done(3, 5000))
    assert sd[0] == p._TAG[p.SLICE_DONE]
    assert struct.unpack_from(">HQ", sd, 1) == (3, 5000)


def test_add_peer_encrypt_carries_lmk_hex():
    lmk = bytes(range(16))
    m = p.add_peer(2, "aa:bb:cc:dd:ee:02", channel=6, encrypt=True, lmk=lmk)
    frame = p.encode_frame(m)
    got = p.FrameDecoder().feed(frame)[0]
    assert got.get("encrypt") is True
    assert p.hex_to_data(got.get("lmk")) == lmk
