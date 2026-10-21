"""SNR -> packet error rate, from the BER formulas ns-3's YansErrorRateModel uses (D-007)."""

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


@dataclass(frozen=True)
class SnrPerErrorModel:
    """Physical model: SNR -> per-modulation BER -> PER = 1-(1-BER)^(8*nbytes)."""

    def per(self, ctx: ReceptionContext) -> float:
        nbits = max(1, ctx.nbytes) * 8
        ebn0 = _ebn0_linear(ctx.snr_db, ctx.rate_mbps, ctx.bandwidth_hz)
        # ESP-NOW goes at 1 Mbps (DBPSK); Wi-Fi at 11 Mbps (CCK), approximated as QPSK.
        ber_fn = ber_dbpsk if ctx.rate_mbps <= 1.0 else ber_qpsk_coherent
        ber = min(max(ber_fn(ebn0), 0.0), 0.5)
        return -math.expm1(nbits * math.log1p(-ber))  # stable 1-(1-BER)^nbits
