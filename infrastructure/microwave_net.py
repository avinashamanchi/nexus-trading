"""
Microwave & millimeter-wave backhaul network simulation.

Models proprietary microwave tower networks (McKay Brothers / Vigilant / Jump)
connecting CME Aurora, IL to Equinix NY4, Secaucus, NJ.

Key physics:
  Speed of light in air:        ~299,704 km/s   (vacuum × 0.9997)
  Speed of light in SMF-28:     ~204,190 km/s   (vacuum / n, n ≈ 1.468)
  CME Aurora → Equinix NY4:     ~1,192 km straight-line
  Microwave one-way latency:    ~3.97 ms
  Fiber one-way latency:        ~6.87 ms  (+ routing overhead ×1.18)
  Microwave advantage:          ~2.9 ms one-way

Bandwidth/reliability constraints vs fiber:
  Bandwidth:     ~1 Gbps (microwave) vs 100 Gbps (fiber)
  Packet loss:   0.01 % clear, up to 100 % in heavy rain (rain fade)
  Hop distance:  ~80 km max at 12 GHz before free-space path loss is fatal

Use case: Agent 21 (Stat Arb) receives the CME ES futures tick via microwave
~3 ms before any fiber-connected competitor, then arbitrages SPY/QQQ in NY4.
"""
from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple

# ─── Physical Constants ───────────────────────────────────────────────────────

SPEED_OF_LIGHT_KM_S  = 299_704.0    # km/s in air
SPEED_OF_FIBER_KM_S  = 204_190.0    # km/s in SMF-28 (n ≈ 1.468)
FIBER_PATH_OVERHEAD  = 1.18         # route-vs-straight-line overhead

CME_TO_NY4_KM = 1_192.0

MICROWAVE_ONE_WAY_MS = CME_TO_NY4_KM / SPEED_OF_LIGHT_KM_S * 1_000   # ~3.97 ms
FIBER_ONE_WAY_MS     = CME_TO_NY4_KM * FIBER_PATH_OVERHEAD / SPEED_OF_FIBER_KM_S * 1_000  # ~6.87 ms
MICROWAVE_ADVANTAGE_MS = FIBER_ONE_WAY_MS - MICROWAVE_ONE_WAY_MS      # ~2.9 ms


class WeatherCondition(str, Enum):
    CLEAR      = "clear"
    OVERCAST   = "overcast"
    LIGHT_RAIN = "light_rain"
    HEAVY_RAIN = "heavy_rain"
    FOG        = "fog"


# Rain-fade additional attenuation at 12 GHz (dB)
RAIN_FADE_DB: dict[WeatherCondition, float] = {
    WeatherCondition.CLEAR:      0.0,
    WeatherCondition.OVERCAST:   0.5,
    WeatherCondition.LIGHT_RAIN: 3.0,
    WeatherCondition.HEAVY_RAIN: 12.0,
    WeatherCondition.FOG:        1.5,
}


# ─── Single Hop ───────────────────────────────────────────────────────────────

@dataclass
class MicrowaveHop:
    """
    One point-to-point tower link in the microwave chain.

    Max hop distance at 12 GHz ≈ 80 km (free-space path loss budget).
    The CME→NY4 McKay Brothers route uses ≈ 15 hops.

    Real links use high-gain parabolic dish antennas (~35 dBi each) to overcome
    free-space path loss.  System gain = TX antenna gain + RX antenna gain.
    """
    hop_id:             int
    distance_km:        float
    frequency_ghz:      float = 12.0
    tx_power_dbm:       float = 30.0
    antenna_gain_dbi:   float = 35.0   # each end; typical parabolic dish
    rx_sensitivity_dbm: float = -80.0

    @property
    def propagation_us(self) -> float:
        """One-way propagation delay in microseconds."""
        return self.distance_km / SPEED_OF_LIGHT_KM_S * 1_000_000

    @property
    def free_space_loss_db(self) -> float:
        """Friis FSPL = 20·log10(4π·d·f / c)."""
        f_hz = self.frequency_ghz * 1e9
        c_ms = SPEED_OF_LIGHT_KM_S * 1e3   # m/s
        d_m  = self.distance_km * 1e3
        fspl = (4 * math.pi * d_m * f_hz / c_ms) ** 2
        return 10 * math.log10(max(fspl, 1e-30))

    @property
    def link_margin_db(self) -> float:
        """Margin = TX + TX_ant + RX_ant − FSPL − RX_sensitivity."""
        received_dbm = (self.tx_power_dbm
                        + 2 * self.antenna_gain_dbi
                        - self.free_space_loss_db)
        return received_dbm - self.rx_sensitivity_dbm

    def packet_loss_rate(self, weather: WeatherCondition) -> float:
        """Probability [0,1] that a single frame is lost at this hop."""
        margin = self.link_margin_db - RAIN_FADE_DB[weather]
        if margin >= 10.0:
            return 0.0001    # 0.01 % baseline (atmospheric scintillation)
        elif margin >= 5.0:
            return 0.001
        elif margin >= 0.0:
            return 0.02      # marginal link
        else:
            return 1.0       # link down


# ─── Route (chain of hops) ────────────────────────────────────────────────────

class MicrowavePacket(NamedTuple):
    seq:         int
    payload:     bytes
    origin_ns:   int
    origin:      str
    destination: str


@dataclass
class MicrowaveRoute:
    """
    End-to-end microwave route: CME Aurora → Equinix NY4.
    A chain of MicrowaveHop objects; packet loss is independent per hop.
    """
    name:                str
    hops:                list[MicrowaveHop] = field(default_factory=list)
    max_bandwidth_gbps:  float              = 1.0
    weather:             WeatherCondition   = WeatherCondition.CLEAR

    @classmethod
    def cme_to_ny4(cls, weather: WeatherCondition = WeatherCondition.CLEAR) -> "MicrowaveRoute":
        """Build the canonical 15-hop CME Aurora → Equinix NY4 route."""
        n_hops = 15
        hop_km = CME_TO_NY4_KM / n_hops
        hops = [MicrowaveHop(hop_id=i, distance_km=hop_km) for i in range(n_hops)]
        return cls(name="CME_Aurora→Equinix_NY4", hops=hops, weather=weather)

    @property
    def total_propagation_ms(self) -> float:
        return sum(h.propagation_us for h in self.hops) / 1_000

    @property
    def fiber_advantage_ms(self) -> float:
        return FIBER_ONE_WAY_MS - self.total_propagation_ms

    def packet_delivery_probability(self) -> float:
        """P(packet arrives) = product over all hops of P(hop OK)."""
        prob = 1.0
        for hop in self.hops:
            prob *= 1.0 - hop.packet_loss_rate(self.weather)
        return prob

    async def transmit(self, packet: MicrowavePacket) -> MicrowavePacket | None:
        """
        Simulate packet transmission across all hops.
        Returns None if the packet is lost due to rain fade / path loss.
        Adds propagation latency via asyncio.sleep.
        """
        for hop in self.hops:
            if random.random() < hop.packet_loss_rate(self.weather):
                return None

        await asyncio.sleep(self.total_propagation_ms / 1_000)
        return packet

    def summary(self) -> dict:
        return {
            "route":            self.name,
            "latency_ms":       round(self.total_propagation_ms, 3),
            "fiber_ms":         round(FIBER_ONE_WAY_MS, 3),
            "advantage_ms":     round(self.fiber_advantage_ms, 3),
            "hops":             len(self.hops),
            "weather":          self.weather.value,
            "delivery_prob":    round(self.packet_delivery_probability(), 5),
            "bandwidth_gbps":   self.max_bandwidth_gbps,
        }


# ─── Arbitrage Edge Tracker ───────────────────────────────────────────────────

class MicrowaveArbitrageEdge:
    """
    Tracks statistical-arbitrage P&L generated by microwave speed advantage.

    When ES futures tick up in Chicago, SPY/QQQ will follow in NJ.
    The first firm to receive the Chicago signal arbitrages that move.
    Microwave head-start ≈ 2.9 ms one-way → first-mover advantage.
    """

    def __init__(self, route: MicrowaveRoute) -> None:
        self._route = route
        self._ticks_arbed = 0
        self._total_pnl_usd = 0.0

    def record_arbitrage(self, notional_usd: float, edge_bps: float) -> float:
        """Record one arbitrage event. Returns estimated P&L."""
        pnl = notional_usd * edge_bps / 10_000
        self._ticks_arbed += 1
        self._total_pnl_usd += pnl
        return pnl

    @property
    def advantage_ms(self) -> float:
        return self._route.fiber_advantage_ms

    def summary(self) -> dict:
        return {
            "route":           self._route.name,
            "advantage_ms":    round(self.advantage_ms, 3),
            "ticks_arbitraged": self._ticks_arbed,
            "total_pnl_usd":   round(self._total_pnl_usd, 2),
        }
