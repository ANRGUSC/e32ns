"""802.11 DCF airtime: PLCP + DIFS/SIFS + backoff + frame + ACK, in microseconds (D-008)."""

from __future__ import annotations

from dataclasses import dataclass

from .units import SPEED_OF_LIGHT

# DSSS (802.11b, 2.4 GHz) DCF timing constants.
SLOT_US = 20
SIFS_US = 10
DIFS_US = SIFS_US + 2 * SLOT_US
LONG_PLCP_US = 192          # long preamble (144) + PLCP header (48)
MAC_HEADER_BYTES = 24
FCS_BYTES = 4
ACK_BYTES = 14
DEFAULT_CW_MIN = 15         # 802.11b aCWmin (slots)


@dataclass(frozen=True)
class AirtimeModel:
    slot_us: int = SLOT_US
    sifs_us: int = SIFS_US
    difs_us: int = DIFS_US
    plcp_us: int = LONG_PLCP_US
    mac_header_bytes: int = MAC_HEADER_BYTES
    fcs_bytes: int = FCS_BYTES
    ack_rate_mbps: float = 1.0
    cw_min: int = DEFAULT_CW_MIN

    def _phy_frame_us(self, payload_bytes: int, rate_mbps: float) -> float:
        octets = self.mac_header_bytes + payload_bytes + self.fcs_bytes
        return self.plcp_us + octets * 8.0 / rate_mbps  # us = bits / (Mbps)

    def _ack_us(self) -> float:
        return self.plcp_us + ACK_BYTES * 8.0 / self.ack_rate_mbps

    def ack_tail_us(self) -> int:
        """The SIFS+ACK part of an acknowledged airtime, after the data frame lands."""
        return int(round(self.sifs_us + self._ack_us()))

    def backoff_us(self, rng) -> int:
        return rng.randint(0, self.cw_min) * self.slot_us

    def tx_airtime_us(self, payload_bytes: int, rate_mbps: float,
                      acknowledged: bool, rng=None) -> int:
        """Medium occupancy: DIFS+backoff+frame, plus SIFS+ACK when acknowledged (unicast)."""
        backoff = self.backoff_us(rng) if rng is not None else 0
        total = self.difs_us + backoff + self._phy_frame_us(payload_bytes, rate_mbps)
        if acknowledged:
            total += self.sifs_us + self._ack_us()
        return int(round(total))

    def propagation_delay_us(self, distance_m: float) -> int:
        return int(round(distance_m / SPEED_OF_LIGHT * 1e6))
