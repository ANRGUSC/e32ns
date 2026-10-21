"""RF unit conversions and small signal-processing helpers."""

from __future__ import annotations

import math

SPEED_OF_LIGHT = 299_792_458.0
BOLTZMANN = 1.380649e-23
# kT at 290 K in dBm/Hz: 10*log10(k*T*1000) = -173.98 (commonly quoted as -174).
THERMAL_NOISE_DBM_PER_HZ = 10.0 * math.log10(BOLTZMANN * 290.0 * 1000.0)


def dbm_to_mw(dbm: float) -> float:
    return 10.0 ** (dbm / 10.0)


def mw_to_dbm(mw: float) -> float:
    return -math.inf if mw <= 0.0 else 10.0 * math.log10(mw)


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 10.0)


def friis_loss_1m_db(freq_hz: float) -> float:
    """Free-space (Friis) loss at 1 m; ~40.046 dB at 2.4 GHz, matching ns-3's 40.0459."""
    return 20.0 * math.log10(4.0 * math.pi * freq_hz / SPEED_OF_LIGHT)


def thermal_noise_dbm(bandwidth_hz: float, noise_figure_db: float = 0.0) -> float:
    """Receiver noise floor: kTB (dBm) + noise figure."""
    return THERMAL_NOISE_DBM_PER_HZ + 10.0 * math.log10(bandwidth_hz) + noise_figure_db


def qfunc(x: float) -> float:
    """Gaussian Q-function 0.5*erfc(x/sqrt(2))."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))
