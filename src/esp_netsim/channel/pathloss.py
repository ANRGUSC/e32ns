"""Log-distance path loss with optional log-normal shadowing (D-006)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from . import units

FREQ_2_4_GHZ = 2_400_000_000.0


@dataclass(frozen=True)
class LogDistancePropagationLoss:
    """Path loss PL(d) = PL(d0) + 10*n*log10(d/d0) + X_sigma."""

    exponent: float = 3.0
    d0_m: float = 1.0
    pl_d0_db: Optional[float] = None
    sigma_db: float = 0.0
    freq_hz: float = FREQ_2_4_GHZ

    def _reference_loss(self) -> float:
        if self.pl_d0_db is not None:
            return self.pl_d0_db
        return units.friis_reference_loss_db(self.freq_hz, self.d0_m)

    def path_loss_db(self, distance_m: float, rng=None) -> float:
        d = max(distance_m, self.d0_m)  # clamp: no gain closer than the reference distance
        pl = self._reference_loss() + 10.0 * self.exponent * math.log10(d / self.d0_m)
        if self.sigma_db > 0.0 and rng is not None:
            pl += rng.gauss(0.0, self.sigma_db)
        return pl


@dataclass(frozen=True)
class LinkBudget:
    """Received power Prx = Ptx + Gtx + Grx - PL(d)."""

    tx_power_dbm: float = 20.0
    tx_gain_dbi: float = 0.0
    rx_gain_dbi: float = 0.0

    def rx_power_dbm(self, loss: LogDistancePropagationLoss, distance_m: float,
                     rng=None) -> float:
        return (self.tx_power_dbm + self.tx_gain_dbi + self.rx_gain_dbi
                - loss.path_loss_db(distance_m, rng))
