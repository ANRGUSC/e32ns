"""SNR/geometry -> packet error rate (D-007).

SnrPerErrorModel uses the standard digital-comms BER formulas ns-3's YansErrorRateModel
is built on; it is validated for invariants and reference points, not bit-exact ns-3 parity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from . import units


@dataclass(frozen=True)
class ReceptionContext:
    snr_db: float
    nbytes: int
    rate_mbps: float
    distance_m: float = 0.0
    bandwidth_hz: float = 22_000_000.0


class ErrorRateModel(Protocol):
    def per(self, ctx: ReceptionContext) -> float: ...


@dataclass(frozen=True)
class ConstantErrorModel:
    """Fixed packet-error rate, independent of SNR/geometry."""

    packet_error_rate: float = 0.0

    def per(self, ctx: ReceptionContext) -> float:
        return self.packet_error_rate


def _ebn0_linear(snr_db: float, rate_mbps: float, bandwidth_hz: float) -> float:
    """Eb/N0 = SNR * (B / R)."""
    return units.db_to_linear(snr_db) * (bandwidth_hz / (rate_mbps * 1_000_000.0))


def ber_dbpsk(ebn0: float) -> float:
    """DBPSK (802.11b 1 Mbps): Pb = 0.5*exp(-Eb/N0)."""
    return 0.5 * math.exp(-ebn0)


def ber_qpsk_coherent(ebn0: float) -> float:
    """Coherent QPSK per-bit BER = Q(sqrt(2*Eb/N0))."""
    return units.qfunc(math.sqrt(2.0 * ebn0))


def ber_mqam(ebn0: float, m: int) -> float:
    """Gray-coded square M-QAM approximate BER (M in {4,16,64}), BPSK for M<=2."""
    if m <= 2:
        return units.qfunc(math.sqrt(2.0 * ebn0))
    k = math.log2(m)
    coeff = (4.0 / k) * (1.0 - 1.0 / math.sqrt(m))
    arg = math.sqrt(3.0 * k / (m - 1) * ebn0)
    return min(0.5, coeff * units.qfunc(arg))


def _modulation_for_rate(rate_mbps: float):
    """BER function for an 802.11b/g data rate."""
    if rate_mbps <= 1.0:
        return ber_dbpsk
    if rate_mbps <= 2.0:
        return ber_qpsk_coherent
    if rate_mbps <= 6.0:
        return lambda e: ber_mqam(e, 2)
    if rate_mbps <= 12.0:
        return lambda e: ber_mqam(e, 4)
    if rate_mbps <= 24.0:
        return lambda e: ber_mqam(e, 16)
    return lambda e: ber_mqam(e, 64)


@dataclass(frozen=True)
class SnrPerErrorModel:
    """Physical model: SNR -> per-modulation BER -> PER = 1-(1-BER)^(8*nbytes)."""

    def per(self, ctx: ReceptionContext) -> float:
        nbits = max(1, ctx.nbytes) * 8
        ebn0 = _ebn0_linear(ctx.snr_db, ctx.rate_mbps, ctx.bandwidth_hz)
        ber = min(max(_modulation_for_rate(ctx.rate_mbps)(ebn0), 0.0), 0.5)
        return -math.expm1(nbits * math.log1p(-ber))  # stable 1-(1-BER)^nbits
