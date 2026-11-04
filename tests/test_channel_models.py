"""The radio model against reference constants, physical invariants and hand-computed points."""

import math
import random

import pytest

from esp_netsim import channel as ch


def test_noise_floor_reference():
    assert ch.THERMAL_NOISE_DBM_PER_HZ == pytest.approx(-173.98, abs=0.02)   # textbook -174
    assert ch.noise_floor_dbm(0.0) == pytest.approx(-100.56, abs=0.1)        # 22 MHz, 0 dB NF


def test_qfunc_known_points():
    assert ch.qfunc(0.0) == pytest.approx(0.5, abs=1e-12)
    assert ch.qfunc(1.0) == pytest.approx(0.158655, abs=1e-5)
    assert ch.qfunc(2.0) == pytest.approx(0.022750, abs=1e-5)


def test_log_distance_reference_exponent_and_clamp():
    assert ch.FRIIS_LOSS_1M_DB == pytest.approx(40.046, abs=0.01)   # ns-3's 40.0459 dB
    m = ch.LogDistance(exponent=3.0, tx_power_dbm=20.0)
    assert m.rx_power_dbm(1.0) == pytest.approx(20.0 - 40.046, abs=0.01)
    assert m.rx_power_dbm(10.0) - m.rx_power_dbm(100.0) == pytest.approx(30.0, abs=1e-9)
    assert m.rx_power_dbm(0.0) == m.rx_power_dbm(1.0)


def test_dbpsk_per_matches_hand_computed_point():
    # SNR such that Eb/N0 = 1 at 1 Mbps over 22 MHz: BER = 0.5/e, PER over one byte.
    snr_db = 10 * math.log10(1.0 / 22.0)
    ber = 0.5 * math.exp(-1.0)
    assert ch.SnrPerErrorModel().per(snr_db, 1, 1.0) == pytest.approx(1 - (1 - ber) ** 8)


def test_snr_per_is_monotone_and_bounded():
    m = ch.SnrPerErrorModel()
    pers = [m.per(float(snr), 100, 1.0) for snr in range(-10, 41, 2)]
    assert all(0.0 <= p <= 1.0 for p in pers)
    assert all(a >= b - 1e-12 for a, b in zip(pers, pers[1:]))
    assert pers[0] > 0.99 and pers[-1] < 1e-3
    assert m.per(6.0, 250, 1.0) >= m.per(6.0, 10, 1.0)   # longer frames lose more


def test_airtime_scales_with_payload_and_ack():
    def airtime(nbytes, acked):
        return ch.airtime_us(nbytes, 1.0, acked, random.Random(7))   # same backoff each call
    assert 300 <= airtime(1, True) <= 1500           # same order as the measured 1,059 us
    assert airtime(100, True) - airtime(1, True) == 99 * 8
    assert airtime(50, True) - airtime(50, False) == ch.ACK_TAIL_US
