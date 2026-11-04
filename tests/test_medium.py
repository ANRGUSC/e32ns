"""The medium's verdicts: ESP-NOW send-callback semantics, collisions, carrier sense, links."""

from esp_netsim.channel import ACK_TAIL_US, ConstantErrorModel, SnrPerErrorModel
from esp_netsim.medium import MediumEngine, MediumConfig, Frame, Link
from esp_netsim.protocol import BROADCAST_MAC


def make_engine(error_model=None, **cfg_kw):
    cfg = MediumConfig(error_model=error_model or ConstantErrorModel(0.0), **cfg_kw)
    return MediumEngine(cfg)


MAC1 = "aa:bb:cc:00:00:01"
MAC2 = "aa:bb:cc:00:00:02"
MAC3 = "aa:bb:cc:00:00:03"


def frame(src_node, src_mac, dst_mac, payload=b"hello", start=0, ch=1, rate=1.0, seq=1):
    return Frame(src_node, src_mac, dst_mac, ch, rate, seq, payload, start)


# ---- unicast send-callback semantics ---------------------

def test_unicast_success_when_deliverable():
    eng = make_engine()
    eng.add_node(1, MAC1, x=0, y=0)
    eng.add_node(2, MAC2, x=1, y=0)
    out = eng.transmit(frame(1, MAC1, MAC2))
    assert out.status == "success"
    assert out.reason == "acked"
    assert len(out.receivers) == 1 and out.receivers[0].node_id == 2
    assert out.receivers[0].delivered


def test_unicast_to_absent_mac_sends_every_attempt():
    eng = make_engine()
    eng.add_node(1, MAC1)
    out = eng.transmit(frame(1, MAC1, MAC2))  # no node has MAC2
    assert out.status == "fail" and out.reason == "no_receiver"
    assert out.attempts == 35


def test_neighbour_defers_through_whole_retry_sequence():
    eng = make_engine()
    eng.add_node(1, MAC1, x=0)
    eng.add_node(2, MAC2, x=5)
    lost = eng.transmit(frame(1, MAC1, MAC3))  # no node has MAC3
    out = eng.transmit(frame(2, MAC2, BROADCAST_MAC, start=lost.airtime_us // 2))
    assert out.frame.start_us == lost.airtime_us


def test_unicast_fail_lost_on_air_uses_retries():
    # PER = 1.0 => every attempt lost => fail after max_retries+1 attempts.
    eng = make_engine(error_model=ConstantErrorModel(1.0), max_retries=3)
    eng.add_node(1, MAC1)
    eng.add_node(2, MAC2, x=1)
    out = eng.transmit(frame(1, MAC1, MAC2))
    assert out.status == "fail" and out.reason == "lost_on_air"
    assert out.attempts == 4  # 1 + 3 retries


# ---- broadcast semantics ---------------------------------

def test_broadcast_success_and_multi_receiver():
    eng = make_engine()
    eng.add_node(1, MAC1, x=0, y=0)
    eng.add_node(2, MAC2, x=1, y=0)
    eng.add_node(3, MAC3, x=2, y=0)
    out = eng.transmit(frame(1, MAC1, BROADCAST_MAC))
    assert out.status == "success" and out.reason == "broadcast_unacked"
    delivered = [r for r in out.receivers if r.delivered]
    assert {r.node_id for r in delivered} == {2, 3}


def test_broadcast_skips_other_channel():
    eng = make_engine()
    eng.add_node(1, MAC1, channel=1)
    eng.add_node(2, MAC2, channel=1)
    eng.add_node(3, MAC3, channel=6)  # different channel
    out = eng.transmit(frame(1, MAC1, BROADCAST_MAC, ch=1))
    delivered_ids = {r.node_id for r in out.receivers if r.delivered}
    assert delivered_ids == {2}


# ---- encryption policy -----------------------------------

def test_encrypted_unicast_requires_matching_lmk():
    eng = make_engine()
    eng.add_node(1, MAC1, x=0)
    eng.add_node(2, MAC2, x=1)
    lmk = bytes(range(16))
    eng.add_peer(1, MAC2, encrypt=True, lmk=lmk)
    out = eng.transmit(frame(1, MAC1, MAC2, start=0))
    assert out.status == "fail" and out.reason == "encrypt_no_matching_lmk"
    eng.add_peer(2, MAC1, encrypt=True, lmk=lmk)
    assert eng.transmit(frame(1, MAC1, MAC2, start=100_000)).status == "success"
    eng.del_peer(2, MAC1)
    assert eng.transmit(frame(1, MAC1, MAC2, start=200_000)).reason == "encrypt_no_matching_lmk"


# ---- collision / SINR ------------------------------

def test_too_weak_to_hear_is_not_a_collision():
    # Node 4 is 1 km from node 1: below sensitivity whether or not node 2 is also on the air.
    eng = make_engine()
    eng.add_node(1, MAC1, x=0)
    eng.add_node(2, MAC2, x=1000)
    eng.add_node(4, "aa:bb:cc:00:00:04", x=1000.5)
    outs = eng.process_batch([frame(1, MAC1, BROADCAST_MAC), frame(2, MAC2, BROADCAST_MAC)])
    assert [r.reason for r in outs[0].receivers if r.node_id == 4] == ["below_sensitivity"]


def test_hidden_terminals_collide_at_shared_receiver():
    # 140 m apart the senders cannot hear each other (-84 dBm < -82 dBm CCA), so both
    # transmit at once; SINR ~ 0 dB at the receiver in the middle -> collision.
    eng = make_engine(capture_threshold_db=6.0)
    eng.add_node(1, MAC1, x=-70, y=0)
    eng.add_node(2, MAC2, x=70, y=0)
    eng.add_node(3, MAC3, x=0, y=0)
    f1 = frame(1, MAC1, BROADCAST_MAC, start=0)
    f2 = frame(2, MAC2, BROADCAST_MAC, start=0)
    outs = eng.process_batch([f1, f2])
    # Receiver node 3's verdict for each frame should be a collision.
    for out in outs:
        v3 = [r for r in out.receivers if r.node_id == 3][0]
        assert not v3.delivered
        assert v3.reason == "collision"


def test_unicast_retry_escapes_a_hidden_terminal_collision():
    eng = make_engine(capture_threshold_db=6.0)
    eng.add_node(1, MAC1, x=-70, y=0)
    eng.add_node(2, MAC2, x=70, y=0)
    eng.add_node(3, MAC3, x=0, y=0)
    unicast, _ = eng.process_batch([frame(1, MAC1, MAC3, start=0),
                                    frame(2, MAC2, BROADCAST_MAC, start=0)])
    # The first attempt collides; the retry goes out after the broadcast has ended.
    assert (unicast.status, unicast.attempts) == ("success", 2)


def test_senders_in_carrier_sense_range_take_turns():
    eng = make_engine(capture_threshold_db=6.0)
    eng.add_node(1, MAC1, x=-10, y=0)
    eng.add_node(2, MAC2, x=10, y=0)
    eng.add_node(3, MAC3, x=0, y=0)
    first, second = eng.process_batch([frame(1, MAC1, BROADCAST_MAC, start=0),
                                       frame(2, MAC2, BROADCAST_MAC, start=0)])
    assert second.frame.start_us == first.frame.start_us + first.airtime_us
    for out in (first, second):
        assert [r.delivered for r in out.receivers if r.node_id == 3] == [True]


def test_non_overlapping_transmissions_do_not_collide():
    eng = make_engine(capture_threshold_db=6.0)
    eng.add_node(1, MAC1, x=-10, y=0)
    eng.add_node(2, MAC2, x=10, y=0)
    eng.add_node(3, MAC3, x=0, y=0)
    # Second frame starts well after the first frame's airtime (~1 ms later).
    f1 = frame(1, MAC1, BROADCAST_MAC, start=0)
    f2 = frame(2, MAC2, BROADCAST_MAC, start=100_000)
    outs = eng.process_batch([f1, f2])
    for out in outs:
        v3 = [r for r in out.receivers if r.node_id == 3][0]
        assert v3.delivered  # no interference -> delivered


# ---- determinism ---------------------------------------------------

def test_determinism_same_seed_same_result():
    def run():
        eng = make_engine(SnrPerErrorModel(), seed=42)
        eng.add_node(1, MAC1, x=0, y=0)
        eng.add_node(2, MAC2, x=40, y=0)
        return [eng.transmit(frame(1, MAC1, MAC2, seq=i, start=i * 5000)).status
                for i in range(20)]
    assert run() == run()


def test_frame_rejected_before_transmission_does_not_interfere():
    eng = make_engine(capture_threshold_db=6.0)
    eng.add_node(1, MAC1, x=-10, y=0)
    eng.add_node(2, MAC2, x=10, y=0)
    eng.add_node(3, MAC3, x=0, y=0)
    eng.add_node(4, "aa:bb:cc:00:00:09", x=50, y=0, channel=6)
    rejected = eng.transmit(frame(1, MAC1, "aa:bb:cc:00:00:09"))
    assert (rejected.reason, rejected.attempts) == ("channel_mismatch", 0)
    out = eng.transmit(frame(2, MAC2, BROADCAST_MAC))  # same start time
    assert [r.reason for r in out.receivers if r.node_id == 3] == ["ok"]


def test_unicast_rx_lands_before_the_ack_tail():
    eng = make_engine()
    eng.add_node(1, MAC1, x=0, y=0)
    eng.add_node(2, MAC2, x=1, y=0)
    out = eng.transmit(frame(1, MAC1, MAC2))
    assert out.receivers[0].delivery_us == out.airtime_us - ACK_TAIL_US


# ---- per-link overrides ----------------------------------------------------

def test_link_rssi_override_replaces_distance_both_ways():
    eng = make_engine()
    eng.add_node(1, MAC1, x=0)
    eng.add_node(2, MAC2, x=1)
    eng.add_node(3, MAC3, x=2)
    eng.set_link(Link(1, 2, rssi_dbm=-100.0))   # a wall between 1 and 2
    out = eng.transmit(frame(1, MAC1, BROADCAST_MAC))
    by_node = {r.node_id: r for r in out.receivers}
    assert (by_node[2].rssi_dbm, by_node[2].reason) == (-100.0, "below_sensitivity")
    assert by_node[3].delivered
    assert eng.link_quality(2, 1, 50, 1.0) == (-100.0, 1.0)


def test_link_loss_compounds_with_the_channel():
    eng = make_engine(max_retries=4)
    eng.add_node(1, MAC1, x=0)
    eng.add_node(2, MAC2, x=1)
    eng.set_link(Link(1, 2, loss=1.0))
    out = eng.transmit(frame(1, MAC1, MAC2))
    assert (out.status, out.attempts, out.receivers[0].reason) == ("fail", 5, "link_loss")
    assert eng.link_quality(1, 2, 50, 1.0)[1] == 1.0
