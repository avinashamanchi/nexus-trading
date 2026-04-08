"""
Tests for:
  - ExchangeMember       (DMA membership and cross-connect latency)
  - MakerRebateTracker   (per-venue rebate accumulation)
  - DMAOrderPath         (FPGA → exchange submit path)
  - TradeRecord          (signed qty, notional)
  - NetSettlement        (settlement side)
  - DTCCClearingEngine   (netting, fee savings, efficiency)
"""
from __future__ import annotations

import asyncio
import pytest

from core.enums import OrderSide, OrderVenue
from infrastructure.dma_exchange import (
    DTCCClearingEngine,
    DMAOrderPath,
    ExchangeMember,
    MakerRebateTracker,
    NetSettlement,
    SettlementStatus,
    TradeRecord,
    _REBATES_PER_SHARE,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _member(venue: OrderVenue = OrderVenue.NASDAQ) -> ExchangeMember:
    return ExchangeMember(venue=venue, member_id="TEST-001")


def _dma(venues=None) -> DMAOrderPath:
    if venues is None:
        venues = [OrderVenue.NASDAQ, OrderVenue.NYSE, OrderVenue.BATS]
    members = {v: ExchangeMember(venue=v, member_id=f"TEST-{v.value}") for v in venues}
    return DMAOrderPath(members=members)


def _trade(
    symbol="AAPL",
    side=OrderSide.BUY,
    qty=100,
    price=150.0,
    venue=OrderVenue.NASDAQ,
) -> TradeRecord:
    return TradeRecord(
        trade_id=f"T-{symbol}-{side.value}",
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        venue=venue,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  TestExchangeMember
# ═══════════════════════════════════════════════════════════════════════════════

class TestExchangeMember:

    def test_total_path_latency_under_300ns(self):
        m = _member()
        assert m.total_path_latency_ns < 300

    def test_total_path_latency_over_100ns(self):
        m = _member()
        assert m.total_path_latency_ns > 100

    def test_broker_path_latency_is_2ms(self):
        m = _member()
        assert m.broker_path_latency_us == 2_000.0

    def test_dma_faster_than_broker(self):
        m = _member()
        assert m.total_path_latency_ns < m.broker_path_latency_us * 1_000

    def test_is_active_default_true(self):
        m = _member()
        assert m.is_active is True


# ═══════════════════════════════════════════════════════════════════════════════
#  TestMakerRebateTracker
# ═══════════════════════════════════════════════════════════════════════════════

class TestMakerRebateTracker:

    def test_nasdaq_rebate_correct(self):
        t = MakerRebateTracker()
        rebate = t.record_maker_fill(OrderVenue.NASDAQ, 1_000)
        assert rebate == pytest.approx(2.0, rel=1e-6)  # $0.002 × 1000

    def test_bats_highest_rebate(self):
        t = MakerRebateTracker()
        bats_rate  = _REBATES_PER_SHARE[OrderVenue.BATS]
        nasdaq_rate = _REBATES_PER_SHARE[OrderVenue.NASDAQ]
        assert bats_rate > nasdaq_rate

    def test_accumulates_daily_rebate(self):
        t = MakerRebateTracker()
        t.record_maker_fill(OrderVenue.NASDAQ, 500)
        t.record_maker_fill(OrderVenue.NYSE,   500)
        assert t._daily_rebate > 0

    def test_daily_reset_returns_amount(self):
        t = MakerRebateTracker()
        t.record_maker_fill(OrderVenue.NASDAQ, 1_000)
        day = t.daily_reset()
        assert day > 0
        assert t._daily_rebate == 0.0

    def test_total_rebate_persists_after_daily_reset(self):
        t = MakerRebateTracker()
        t.record_maker_fill(OrderVenue.NASDAQ, 1_000)
        t.daily_reset()
        assert t._total_rebate > 0

    def test_summary_has_required_keys(self):
        t = MakerRebateTracker()
        s = t.summary()
        assert "daily_rebate_usd" in s
        assert "total_rebate_usd" in s


# ═══════════════════════════════════════════════════════════════════════════════
#  TestDMAOrderPath
# ═══════════════════════════════════════════════════════════════════════════════

class TestDMAOrderPath:

    @pytest.mark.asyncio
    async def test_submit_returns_ack(self):
        dma = _dma()
        result = await dma.submit_order(
            venue=OrderVenue.NASDAQ, symbol="AAPL",
            side=OrderSide.BUY, qty=100, price=150.0, is_maker=True,
        )
        assert result["status"] == "ack"

    @pytest.mark.asyncio
    async def test_ack_includes_latency_ns(self):
        dma = _dma()
        result = await dma.submit_order(
            venue=OrderVenue.NASDAQ, symbol="AAPL",
            side=OrderSide.BUY, qty=100, price=150.0,
        )
        assert "latency_ns" in result
        assert result["latency_ns"] > 0

    @pytest.mark.asyncio
    async def test_maker_fill_earns_rebate(self):
        dma = _dma()
        result = await dma.submit_order(
            venue=OrderVenue.NASDAQ, symbol="AAPL",
            side=OrderSide.BUY, qty=1_000, price=150.0, is_maker=True,
        )
        assert result["rebate_usd"] > 0

    @pytest.mark.asyncio
    async def test_taker_fill_no_rebate(self):
        dma = _dma()
        result = await dma.submit_order(
            venue=OrderVenue.NASDAQ, symbol="AAPL",
            side=OrderSide.BUY, qty=1_000, price=150.0, is_maker=False,
        )
        assert result["rebate_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_rejected_for_unknown_venue(self):
        dma = _dma(venues=[OrderVenue.NASDAQ])
        result = await dma.submit_order(
            venue=OrderVenue.CME, symbol="ES",
            side=OrderSide.BUY, qty=1, price=5_000.0,
        )
        assert result["status"] == "rejected"

    def test_latency_advantage_positive(self):
        dma = _dma()
        adv = dma.latency_advantage_ns(OrderVenue.NASDAQ)
        assert adv > 0

    @pytest.mark.asyncio
    async def test_summary_tracks_order_count(self):
        dma = _dma()
        await dma.submit_order(OrderVenue.NASDAQ, "A", OrderSide.BUY, 100, 10.0)
        await dma.submit_order(OrderVenue.NYSE,   "B", OrderSide.SELL, 50, 20.0)
        assert dma.summary["orders_submitted"] == 2
        assert dma.summary["orders_acked"] == 2


# ═══════════════════════════════════════════════════════════════════════════════
#  TestTradeRecord
# ═══════════════════════════════════════════════════════════════════════════════

class TestTradeRecord:

    def test_buy_signed_qty_positive(self):
        t = _trade(side=OrderSide.BUY, qty=100)
        assert t.signed_qty == 100

    def test_sell_signed_qty_negative(self):
        t = _trade(side=OrderSide.SELL, qty=100)
        assert t.signed_qty == -100

    def test_notional_correct(self):
        t = _trade(qty=200, price=150.0)
        assert t.notional_usd == pytest.approx(30_000.0)

    def test_default_status_pending(self):
        t = _trade()
        assert t.status == SettlementStatus.PENDING


# ═══════════════════════════════════════════════════════════════════════════════
#  TestDTCCClearingEngine
# ═══════════════════════════════════════════════════════════════════════════════

class TestDTCCClearingEngine:

    def test_submit_trade_returns_clearing_id(self):
        eng = DTCCClearingEngine()
        cid = eng.submit_trade(_trade())
        assert cid.startswith("CLR-")

    def test_net_positions_netting(self):
        """Buy 1000 + sell 800 → net +200 for settlement."""
        eng = DTCCClearingEngine()
        eng.submit_trade(_trade("AAPL", OrderSide.BUY,  1_000, 150.0))
        eng.submit_trade(_trade("AAPL", OrderSide.SELL,   800, 150.0))
        nets = eng.net_positions()
        assert "AAPL" in nets
        assert nets["AAPL"].net_qty == 200

    def test_sell_only_gives_negative_net(self):
        eng = DTCCClearingEngine()
        eng.submit_trade(_trade("TSLA", OrderSide.SELL, 500, 200.0))
        nets = eng.net_positions()
        assert nets["TSLA"].net_qty == -500

    def test_net_settlement_side_receive(self):
        eng = DTCCClearingEngine()
        eng.submit_trade(_trade("MSFT", OrderSide.BUY, 100, 300.0))
        nets = eng.net_positions()
        assert nets["MSFT"].settlement_side == "RECEIVE"

    def test_net_settlement_side_deliver(self):
        eng = DTCCClearingEngine()
        eng.submit_trade(_trade("MSFT", OrderSide.SELL, 100, 300.0))
        nets = eng.net_positions()
        assert nets["MSFT"].settlement_side == "DELIVER"

    def test_fee_savings_positive(self):
        eng = DTCCClearingEngine()
        for _ in range(10):
            eng.submit_trade(_trade(qty=1_000))
        assert eng.fee_savings_vs_broker() > 0

    def test_netting_efficiency_lt1_with_offsets(self):
        """Buy and sell same qty → close to 0 net shares to settle."""
        eng = DTCCClearingEngine()
        eng.submit_trade(_trade("AAPL", OrderSide.BUY,  500, 150.0))
        eng.submit_trade(_trade("AAPL", OrderSide.SELL, 500, 150.0))
        eff = eng.netting_efficiency()
        assert eff == 0.0   # perfect netting: buy 500 = sell 500 → net 0

    def test_netting_efficiency_1_with_one_sided(self):
        """All buys → no netting, efficiency = 1.0."""
        eng = DTCCClearingEngine()
        eng.submit_trade(_trade("AAPL", OrderSide.BUY, 100, 150.0))
        eng.submit_trade(_trade("MSFT", OrderSide.BUY, 100, 300.0))
        eff = eng.netting_efficiency()
        assert eff == 1.0

    def test_daily_reset_clears_trades(self):
        eng = DTCCClearingEngine()
        eng.submit_trade(_trade())
        eng.daily_reset()
        assert len(eng._trades) == 0

    def test_summary_has_required_keys(self):
        eng = DTCCClearingEngine()
        s = eng.summary()
        for key in ("member_id", "pending_trades", "symbols_to_settle",
                    "netting_efficiency", "fee_savings_usd", "guarantee_fund_usd"):
            assert key in s

    def test_multi_symbol_netting(self):
        eng = DTCCClearingEngine()
        eng.submit_trade(_trade("AAPL", OrderSide.BUY,  300, 150.0))
        eng.submit_trade(_trade("TSLA", OrderSide.SELL, 200, 200.0))
        nets = eng.net_positions()
        assert len(nets) == 2
