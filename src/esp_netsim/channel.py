"""The radio model: log-distance path loss, SNR to packet error rate, and 802.11 DCF airtime."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Protocol

SPEED_OF_LIGHT = 299_792_458.0
BANDWIDTH_HZ = 22_000_000.0                      # DSSS channel width
ESPNOW_RATE_MBPS = 1.0                           # measured on boards: 8 us per byte (V-014)
THERMAL_NOISE_DBM_PER_HZ = 10.0 * math.log10(1.380649e-23 * 290.0 * 1000.0)   # kT at 290 K
# Free-space loss at 1 m and 2.4 GHz, 40.046 dB: ns-3's LogDistance reference loss (D-006).
FRIIS_LOSS_1M_DB = 20.0 * math.log10(4.0 * math.pi * 2_400_000_000.0 / SPEED_OF_LIGHT)

# 802.11b DSSS DCF timing (D-008).
SLOT_US = 20
SIFS_US = 10
DIFS_US = SIFS_US + 2 * SLOT_US
PLCP_US = 192                                    # long preamble (144) + PLCP header (48)
CW_MIN = 15                                      # aCWmin, in slots
MAC_OVERHEAD_BYTES = 24 + 4                      # MAC header + FCS
_ACK_US = PLCP_US + 14 * 8.0 / 1.0               # a 14-byte ACK at 1 Mbps
ACK_TAIL_US = int(round(SIFS_US + _ACK_US))      # SIFS + ACK, after an acknowledged data frame


def from_db(db: float) -> float:
    """dB to a linear ratio, or dBm to mW."""
    return 10.0 ** (db / 10.0)


def to_dbm(mw: float) -> float:
    return 10.0 * math.log10(mw)


def noise_floor_dbm(noise_figure_db: float) -> float:
    """Receiver noise floor over the channel: kTB plus the noise figure."""
    return THERMAL_NOISE_DBM_PER_HZ + 10.0 * math.log10(BANDWIDTH_HZ) + noise_figure_db


def qfunc(x: float) -> float:
    """The Gaussian tail probability Q(x)."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


@dataclass(frozen=True)
class LogDistance:
    """Prx(d) = Ptx - PL(1 m) - 10*n*log10(d / 1 m), with no gain closer than 1 m."""

    exponent: float = 3.0
    tx_power_dbm: float = 20.0

    def rx_power_dbm(self, distance_m: float) -> float:
        return (self.tx_power_dbm - FRIIS_LOSS_1M_DB
                - 10.0 * self.exponent * math.log10(max(distance_m, 1.0)))


class ErrorModel(Protocol):
    def per(self, snr_db: float, nbytes: int, rate_mbps: float) -> float: ...


@dataclass(frozen=True)
class ConstantErrorModel:
    """A fixed packet error rate, whatever the SNR."""

    packet_error_rate: float

    def per(self, snr_db: float, nbytes: int, rate_mbps: float) -> float:
        return self.packet_error_rate


class SnrPerErrorModel:
    """SNR to bit error rate to PER, from the formulas ns-3's YansErrorRateModel uses (D-007)."""

    def per(self, snr_db: float, nbytes: int, rate_mbps: float) -> float:
        ebn0 = from_db(snr_db) * (BANDWIDTH_HZ / (rate_mbps * 1_000_000.0))   # SNR * B / R
        # ESP-NOW goes at 1 Mbps (DBPSK); Wi-Fi at 11 Mbps (CCK) is approximated as coherent QPSK.
        ber = 0.5 * math.exp(-ebn0) if rate_mbps <= 1.0 else qfunc(math.sqrt(2.0 * ebn0))
        return -math.expm1(max(1, nbytes) * 8 * math.log1p(-ber))   # 1 - (1 - BER)^bits, stably


def airtime_us(payload_bytes: int, rate_mbps: float, acknowledged: bool, rng: random.Random) -> int:
    """Medium occupancy: DIFS + backoff + frame, plus SIFS + ACK when acknowledged."""
    frame = PLCP_US + (MAC_OVERHEAD_BYTES + payload_bytes) * 8.0 / rate_mbps
    total = DIFS_US + rng.randint(0, CW_MIN) * SLOT_US + frame
    if acknowledged:
        total += SIFS_US + _ACK_US
    return int(round(total))


def propagation_delay_us(distance_m: float) -> int:
    return int(round(distance_m / SPEED_OF_LIGHT * 1e6))
