"""Log-distance path loss from the free-space loss at 1 m (D-006)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import units

FREQ_2_4_GHZ = 2_400_000_000.0


@dataclass(frozen=True)
class LogDistance:
    """Prx(d) = Ptx - PL(1 m) - 10*n*log10(d / 1 m), with no gain closer than 1 m."""

    exponent: float = 3.0
    tx_power_dbm: float = 20.0

    def rx_power_dbm(self, distance_m: float) -> float:
        return (self.tx_power_dbm - units.friis_loss_1m_db(FREQ_2_4_GHZ)
                - 10.0 * self.exponent * math.log10(max(distance_m, 1.0)))
