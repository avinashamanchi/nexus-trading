"""
Tests for:
  - MicrowaveHop              (single tower link physics)
  - MicrowaveRoute            (end-to-end CME→NY4 chain)
  - MicrowaveArbitrageEdge    (stat-arb speed advantage tracker)
"""
from __future__ import annotations

import asyncio
import pytest

from infrastructure.microwave_net import (
    CME_TO_NY4_KM,
    FIBER_ONE_WAY_MS,
    MICROWAVE_ADVANTAGE_MS,
    MICROWAVE_ONE_WAY_MS,
    SPEED_OF_FIBER_KM_S,
    SPEED_OF_LIGHT_KM_S,
    MicrowaveArbitrageEdge,
    MicrowaveHop,
    MicrowavePacket,
    MicrowaveRoute,
    WeatherCondition,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _hop(km: float = 79.5) -> MicrowaveHop:
    return MicrowaveHop(hop_id=0, distance_km=km)


def _route(weather: WeatherCondition = WeatherCondition.CLEAR) -> MicrowaveRoute:
    return MicrowaveRoute.cme_to_ny4(weather=weather)


def _packet(seq: int = 1) -> MicrowavePacket:
    return MicrowavePacket(seq=seq, payload=b"tick", origin_ns=0,
                           origin="CME", destination="NY4")


# ═══════════════════════════════════════════════════════════════════════════════
#  TestPhysicalConstants
# ═══════════════════════════════════════════════════════════════════════════════

class TestPhysicalConstants:

    def test_speed_of_light_in_air_close_to_vacuum(self):
        """299,704 km/s ≈ 99.97 % of vacuum speed."""
        assert 299_000 < SPEED_OF_LIGHT_KM_S < 300_000

    def test_fiber_slower_than_air(self):
        assert SPEED_OF_FIBER_KM_S < SPEED_OF_LIGHT_KM_S

    def test_microwave_latency_under_5ms(self):
        assert MICROWAVE_ONE_WAY_MS < 5.0

    def test_fiber_latency_under_10ms(self):
        assert FIBER_ONE_WAY_MS < 10.0

    def test_microwave_faster_than_fiber(self):
        assert MICROWAVE_ONE_WAY_MS < FIBER_ONE_WAY_MS

    def test_advantage_positive(self):
        assert MICROWAVE_ADVANTAGE_MS > 0

    def test_advantage_at_least_1ms(self):
        assert MICROWAVE_ADVANTAGE_MS >= 1.0


# ═══════════════════════════════════════════════════════════════════════════════
#  TestMicrowaveHop
# ═══════════════════════════════════════════════════════════════════════════════

class TestMicrowaveHop:

    def test_propagation_us_positive(self):
        hop = _hop(79.5)
        assert hop.propagation_us > 0

    def test_propagation_us_correct_formula(self):
        hop = _hop(100.0)
        expected = 100.0 / SPEED_OF_LIGHT_KM_S * 1_000_000
        assert abs(hop.propagation_us - expected) < 0.001

    def test_free_space_loss_positive(self):
        hop = _hop(79.5)
        assert hop.free_space_loss_db > 0

    def test_link_margin_positive_short_hop(self):
        hop = _hop(10.0)   # short hop → good margin
        assert hop.link_margin_db > 0

    def test_clear_weather_loss_near_zero(self):
        hop = _hop(10.0)
        loss = hop.packet_loss_rate(WeatherCondition.CLEAR)
        assert loss < 0.01

    def test_heavy_rain_increases_loss(self):
        hop = _hop(79.5)
        clear_loss = hop.packet_loss_rate(WeatherCondition.CLEAR)
        rain_loss  = hop.packet_loss_rate(WeatherCondition.HEAVY_RAIN)
        assert rain_loss >= clear_loss


# ═══════════════════════════════════════════════════════════════════════════════
#  TestMicrowaveRoute
# ═══════════════════════════════════════════════════════════════════════════════

class TestMicrowaveRoute:

    def test_cme_to_ny4_has_15_hops(self):
        r = _route()
        assert len(r.hops) == 15

    def test_total_distance_approx_1192km(self):
        r = _route()
        total = sum(h.distance_km for h in r.hops)
        assert abs(total - CME_TO_NY4_KM) < 1.0

    def test_latency_less_than_fiber(self):
        r = _route()
        assert r.total_propagation_ms < FIBER_ONE_WAY_MS

    def test_fiber_advantage_positive(self):
        r = _route()
        assert r.fiber_advantage_ms > 0

    def test_delivery_prob_near_1_in_clear(self):
        r = _route(WeatherCondition.CLEAR)
        assert r.packet_delivery_probability() > 0.99

    def test_delivery_prob_lower_in_heavy_rain(self):
        """Use a marginal link (low TX power, no antenna gain) to exercise rain fade."""
        marginal_hop = MicrowaveHop(
            hop_id=0, distance_km=79.5,
            tx_power_dbm=20.0,   # low TX power
            antenna_gain_dbi=0.0,  # omni antenna → marginal link in rain
        )
        r_clear = MicrowaveRoute("test_clear", [marginal_hop], weather=WeatherCondition.CLEAR)
        r_rain  = MicrowaveRoute("test_rain",  [marginal_hop], weather=WeatherCondition.HEAVY_RAIN)
        assert r_rain.packet_delivery_probability() <= r_clear.packet_delivery_probability()

    def test_summary_has_required_keys(self):
        r = _route()
        s = r.summary()
        for key in ("route", "latency_ms", "fiber_ms", "advantage_ms",
                    "hops", "weather", "delivery_prob"):
            assert key in s

    @pytest.mark.asyncio
    async def test_transmit_returns_packet_in_clear(self):
        r = MicrowaveRoute(
            name="test",
            hops=[MicrowaveHop(hop_id=0, distance_km=1.0)],
            weather=WeatherCondition.CLEAR,
        )
        result = await r.transmit(_packet())
        assert result is not None
        assert result.seq == 1

    @pytest.mark.asyncio
    async def test_transmit_payload_preserved(self):
        r = MicrowaveRoute(
            name="test",
            hops=[MicrowaveHop(hop_id=0, distance_km=1.0)],
            weather=WeatherCondition.CLEAR,
        )
        pkt = _packet()
        result = await r.transmit(pkt)
        assert result is not None
        assert result.payload == b"tick"


# ═══════════════════════════════════════════════════════════════════════════════
#  TestMicrowaveArbitrageEdge
# ═══════════════════════════════════════════════════════════════════════════════

class TestMicrowaveArbitrageEdge:

    def test_record_arbitrage_returns_pnl(self):
        r    = _route()
        edge = MicrowaveArbitrageEdge(r)
        pnl  = edge.record_arbitrage(notional_usd=1_000_000, edge_bps=0.3)
        assert pnl == pytest.approx(30.0, rel=1e-6)

    def test_advantage_ms_positive(self):
        r    = _route()
        edge = MicrowaveArbitrageEdge(r)
        assert edge.advantage_ms > 0

    def test_summary_has_expected_keys(self):
        r    = _route()
        edge = MicrowaveArbitrageEdge(r)
        edge.record_arbitrage(500_000, 0.2)
        s = edge.summary()
        for key in ("route", "advantage_ms", "ticks_arbitraged", "total_pnl_usd"):
            assert key in s

    def test_multiple_arbs_accumulate_pnl(self):
        r    = _route()
        edge = MicrowaveArbitrageEdge(r)
        for _ in range(10):
            edge.record_arbitrage(100_000, 0.5)
        assert edge.summary()["ticks_arbitraged"] == 10
        assert edge.summary()["total_pnl_usd"] == pytest.approx(50.0, rel=1e-6)
