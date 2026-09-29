"""Tests for the wireless-medium physics (channel/*).

These validate: (a) documented reference constants, (b) physical invariants, and
(c) a few hand-computed points. They do NOT assert bit-exact ns-3 parity (see D-007).
"""

import math
import random

import pytest

from esp_netsim.channel import units
from esp_netsim.channel.pathloss import (
    LogDistancePropagationLoss, LinkBudget, FREQ_2_4_GHZ,
)
from esp_netsim.channel.error_model import (
    ReceptionContext, ConstantErrorModel, SnrPerErrorModel, ber_dbpsk,
)
from esp_netsim.channel.airtime import AirtimeModel, DIFS_US, SIFS_US, SLOT_US


# ---- units -----------------------------------------------------------------

def test_thermal_noise_reference():
    # -174 dBm/Hz is the textbook value; our exact constant is ~-173.98.
    assert units.THERMAL_NOISE_DBM_PER_HZ == pytest.approx(-173.98, abs=0.02)
    # Noise floor over 22 MHz with 0 dB NF ~ -100.6 dBm.
    nf = units.thermal_noise_dbm(22e6, noise_figure_db=0.0)
    assert nf == pytest.approx(-100.56, abs=0.1)


def test_dbm_mw_roundtrip():
    for dbm in (-100.0, -50.0, 0.0, 20.0):
        assert units.mw_to_dbm(units.dbm_to_mw(dbm)) == pytest.approx(dbm, abs=1e-9)


def test_qfunc_known_points():
    assert units.qfunc(0.0) == pytest.approx(0.5, abs=1e-12)
    assert units.qfunc(1.0) == pytest.approx(0.158655, abs=1e-5)
    assert units.qfunc(2.0) == pytest.approx(0.022750, abs=1e-5)


# ---- path loss (D-006) -----------------------------------------------------

def test_friis_reference_matches_ns3_default():
    # brief §5: ns-3 ReferenceLoss for 802.11b at 1 m is 40.0459 dB.
    ref = units.friis_reference_loss_db(FREQ_2_4_GHZ, 1.0)
    assert ref == pytest.approx(40.046, abs=0.01)


def test_log_distance_monotonic_and_exponent():
    m = LogDistancePropagationLoss(exponent=3.0)
    # Each decade of distance adds 10*n = 30 dB.
    pl1 = m.path_loss_db(1.0)
    pl10 = m.path_loss_db(10.0)
    pl100 = m.path_loss_db(100.0)
    assert pl10 - pl1 == pytest.approx(30.0, abs=1e-9)
    assert pl100 - pl10 == pytest.approx(30.0, abs=1e-9)


def test_log_distance_clamps_below_reference():
    m = LogDistancePropagationLoss()
    assert m.path_loss_db(0.0) == m.path_loss_db(1.0)  # no closer than d0


def test_link_budget_rx_power_decreases_with_distance():
    loss = LogDistancePropagationLoss()
    lb = LinkBudget(tx_power_dbm=20.0)
    near = lb.rx_power_dbm(loss, 1.0)
    far = lb.rx_power_dbm(loss, 100.0)
    assert near == pytest.approx(20.0 - 40.046, abs=0.01)
    assert far < near


def test_shadowing_is_deterministic_under_seed():
    m = LogDistancePropagationLoss(sigma_db=4.0)
    a = m.path_loss_db(50.0, random.Random(1))
    b = m.path_loss_db(50.0, random.Random(1))
    assert a == b  # same seed -> same draw (D-011)


# ---- error models (D-007) --------------------------------------------------

def test_constant_error_model():
    m = ConstantErrorModel(0.25)
    ctx = ReceptionContext(snr_db=50, nbytes=100, rate_mbps=1.0)
    assert m.per(ctx) == 0.25


def test_dbpsk_ber_reference():
    # Pb = 0.5 e^{-Eb/N0}. At Eb/N0=1 -> 0.5/e ~= 0.18394.
    assert ber_dbpsk(1.0) == pytest.approx(0.5 * math.exp(-1.0), abs=1e-12)


def test_snr_per_invariants_monotone_and_bounds():
    m = SnrPerErrorModel()
    pers = []
    for snr in range(-10, 41, 2):
        ctx = ReceptionContext(snr_db=float(snr), nbytes=100, rate_mbps=1.0)
        per = m.per(ctx)
        assert 0.0 <= per <= 1.0
        pers.append(per)
    # Monotonically non-increasing in SNR.
    assert all(pers[i] >= pers[i + 1] - 1e-12 for i in range(len(pers) - 1))
    # Very low SNR -> nearly certain loss; very high SNR -> nearly certain success.
    assert pers[0] > 0.99
    assert pers[-1] < 1e-3


def test_snr_per_longer_frames_have_higher_per():
    m = SnrPerErrorModel()
    short = m.per(ReceptionContext(snr_db=6.0, nbytes=10, rate_mbps=1.0))
    long = m.per(ReceptionContext(snr_db=6.0, nbytes=250, rate_mbps=1.0))
    assert long >= short


# ---- airtime (D-008) -------------------------------------------------------

def test_dcf_constants():
    assert SLOT_US == 20 and SIFS_US == 10 and DIFS_US == 50  # brief §5


def test_airtime_order_of_magnitude_matches_espnow_mac():
    # brief §3: ~1 ms MAC latency regime for small ESP-NOW frames at 1 Mbps.
    m = AirtimeModel()
    # No backoff (rng=None) to test the deterministic floor.
    t1 = m.tx_airtime_us(payload_bytes=1, rate_mbps=1.0, acknowledged=True)
    t100 = m.tx_airtime_us(payload_bytes=100, rate_mbps=1.0, acknowledged=True)
    assert 300 <= t1 <= 1500        # same order as measured 1059 us
    assert t100 > t1                # larger payload -> more airtime
    # 99 extra payload bytes at 1 Mbps = 99*8 = 792 us more.
    assert (t100 - t1) == pytest.approx(792, abs=2)


def test_broadcast_has_no_ack_overhead():
    m = AirtimeModel()
    uni = m.tx_airtime_us(50, 1.0, acknowledged=True)
    bcast = m.tx_airtime_us(50, 1.0, acknowledged=False)
    assert uni > bcast  # ACK + SIFS present only for unicast (brief §3)


def test_backoff_is_seeded():
    m = AirtimeModel()
    assert m.backoff_us(random.Random(7)) == m.backoff_us(random.Random(7))
