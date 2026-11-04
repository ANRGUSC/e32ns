"""The wire codec: MAC validation, framing, and the byte layout the C shim mirrors."""

import struct

import pytest

from esp_netsim import protocol as p


def test_normalize_mac():
    assert p.normalize_mac(" AA:BB:CC:DD:EE:FF ") == "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize("bad", ["", "gg:bb:cc:dd:ee:ff", "aa:bb:cc", "a_:bb:cc:dd:ee:ff",
                                 "+a:bb:cc:dd:ee:ff", "aabbccddeeff"])
def test_normalize_mac_rejects_garbage(bad):
    with pytest.raises(ValueError):
        p.normalize_mac(bad)


def _tx(**kw):
    fields = {"node": 2, "t": 0x0102, "src": "aa:bb:cc:dd:ee:01", "dst": p.BROADCAST_MAC,
              "channel": 6, "seq": 9, "data": b"\x01\x02\x03"}
    return p.Message(p.TX, {**fields, **kw})


def test_decoder_reassembles_split_and_coalesced_streams():
    msgs = [p.Message(p.HELLO, {"node": 1, "mac": "aa:bb:cc:dd:ee:01"}), _tx(data=bytes(250)),
            p.Message(p.SLICE_DONE, {"node": 1, "t": 1000})]
    blob = b"".join(p.encode_frame(m) for m in msgs)
    byte_by_byte = p.FrameDecoder()
    out = [m for i in range(len(blob)) for m in byte_by_byte.feed(blob[i:i + 1])]
    assert out == msgs
    assert p.FrameDecoder().feed(blob) == msgs


def test_decoder_rejects_oversize_length_prefix():
    with pytest.raises(ValueError):
        p.FrameDecoder().feed(p.LENGTH_PREFIX.pack(p.MAX_MESSAGE_BYTES + 1))


def test_tx_layout_is_locked():
    # tag | node u16 | t u64 | src 6 | dst 6 | channel u8 | seq u32 | len u16 | data
    body = p.encode_body(_tx())
    assert body[0] == 5
    assert struct.unpack_from(">HQ", body, 1) == (2, 0x0102)
    assert body[11:17] == bytes.fromhex("aabbccddee01") and body[17:23] == b"\xff" * 6
    assert struct.unpack_from(">BIH", body, 23) == (6, 9, 3)
    assert body[30:] == b"\x01\x02\x03"


def test_layouts_the_shim_parses_are_locked():
    # RX: tag | node u16 | t u64 | src 6 | dst 6 | channel u8 | rssi f32 | len u16 | data
    rx = p.encode_body(p.Message(p.RX, {"node": 1, "t": 7, "src": "aa:bb:cc:dd:ee:01",
                                        "dst": p.BROADCAST_MAC, "channel": 6, "rssi_dbm": -60.5,
                                        "data": b"hi"}))
    assert rx[0] == 7 and struct.unpack_from(">Bf H", rx, 23)[:2] == (6, -60.5)
    assert rx[-2:] == b"hi" and len(rx) == 32
    # TX_STATUS: tag | node u16 | seq u32 | status u8
    st = p.encode_body(p.Message(p.TX_STATUS, {"node": 1, "seq": 4, "status": p.STATUS_SUCCESS}))
    assert st == bytes((6,)) + struct.pack(">HIB", 1, 4, 1)


def test_add_peer_carries_encryption():
    lmk = bytes(range(16))
    m = p.Message(p.ADD_PEER, {"node": 2, "peer": "aa:bb:cc:dd:ee:02", "encrypt": True, "lmk": lmk})
    assert p.FrameDecoder().feed(p.encode_frame(m)) == [m]
