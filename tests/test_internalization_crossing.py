"""
Tests for Agent 25 — Dark Pool ATS.

Covers two test classes:
  TestInternalizationCrossing — core crossing logic, midpoint pricing,
    inventory deduction, stats tracking, fee calculations.
  TestPFoFRouting — order flow classification, multi-symbol inventory
    independence, trade ID generation, routing decision labels.

All tests are synchronous (no asyncio).
"""
from __future__ import annotations

import uuid

import pytest

from agents.agent_25_dark_pool import (
    ATSStats,
    CrossedTrade,
    DarkPoolATS,
    EXCHANGE_FEE_PER_SHARE,
    IncomingOrder,
    InventoryPosition,
    OrderFlow,
    PFOF_PAYMENT_PER_SHARE,
    RoutingDecision,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _ats(min_spread_bps: float = 1.0, max_qty: int = 10_000) -> DarkPoolATS:
    """Return a DarkPoolATS loaded with AAPL inventory."""
    ats = DarkPoolATS(min_spread_bps=min_spread_bps, max_internalization_qty=max_qty)
    ats.update_inventory("AAPL", long_qty=10_000, short_qty=0, avg_cost=148.0)
    return ats


def _buy_order(
    qty: int = 100,
    symbol: str = "AAPL",
    flow_type: OrderFlow = OrderFlow.RETAIL,
) -> IncomingOrder:
    return IncomingOrder(
        order_id=uuid.uuid4().hex,
        symbol=symbol,
        side="buy",
        qty=qty,
        limit_price=150.05,
        flow_type=flow_type,
    )


def _sell_order(
    qty: int = 100,
    symbol: str = "AAPL",
    flow_type: OrderFlow = OrderFlow.RETAIL,
) -> IncomingOrder:
    return IncomingOrder(
        order_id=uuid.uuid4().hex,
        symbol=symbol,
        side="sell",
        qty=qty,
        limit_price=150.00,
        flow_type=flow_type,
    )


# Standard quote with ~6.7 bps spread (enough for 1 bps threshold)
_BID = 150.00
_ASK = 150.10


# ─── TestInternalizationCrossing ──────────────────────────────────────────────

class TestInternalizationCrossing:
    """Core crossing engine: eligibility, midpoint pricing, stats, fees."""

    def test_internalize_retail_buy_with_inventory(self):
        """Retail buy with sufficient inventory is internalized."""
        ats = _ats()
        trade = ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.INTERNALIZED

    def test_cross_price_is_midpoint(self):
        """Internalized trade fills exactly at midpoint."""
        ats = _ats()
        trade = ats.route(_buy_order(), bid=_BID, ask=_ASK)
        expected_mid = (_BID + _ASK) / 2.0
        assert trade.cross_price == pytest.approx(expected_mid, rel=1e-9)

    def test_lit_market_for_arb_flow(self):
        """Arbitrage order flow is always routed to lit market, never internalized."""
        ats = _ats()
        order = _buy_order(flow_type=OrderFlow.ARBITRAGE)
        trade = ats.route(order, bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.LIT_MARKET

    def test_lit_market_when_no_inventory(self):
        """No inventory for the symbol forces lit-market routing."""
        ats = DarkPoolATS(min_spread_bps=1.0)
        # No inventory loaded for AAPL
        trade = ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.LIT_MARKET

    def test_lit_market_when_qty_exceeds_max(self):
        """Order exceeding max_internalization_qty is routed to lit market."""
        ats = DarkPoolATS(max_internalization_qty=50, min_spread_bps=1.0)
        ats.update_inventory("AAPL", long_qty=10_000, short_qty=0, avg_cost=148.0)
        trade = ats.route(_buy_order(qty=100), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.LIT_MARKET

    def test_lit_market_when_spread_too_tight(self):
        """Spread below min_spread_bps prevents internalization."""
        # Require 10 bps; spread = 0.10/150.05*10_000 ≈ 6.7 bps < 10
        ats = DarkPoolATS(min_spread_bps=10.0)
        ats.update_inventory("AAPL", long_qty=1_000, short_qty=0, avg_cost=148.0)
        trade = ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.LIT_MARKET

    def test_internalization_rate_after_5_internalizations(self):
        """5 internalized / 5 total → rate == 1.0."""
        ats = _ats()
        for _ in range(5):
            ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert ats.internalization_rate() == pytest.approx(1.0)

    def test_internalization_rate_zero_when_no_internalized(self):
        """Rate is 0.0 when all orders route to lit market."""
        ats = DarkPoolATS(min_spread_bps=1.0)
        # No inventory loaded — all orders will be lit-routed
        for _ in range(3):
            ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert ats.internalization_rate() == pytest.approx(0.0)

    def test_pfof_payment_correct(self):
        """PFoF payment = PFOF_PAYMENT_PER_SHARE × qty."""
        qty = 200
        ats = _ats()
        trade = ats.route(_buy_order(qty=qty), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.INTERNALIZED
        assert trade.pfof_payment == pytest.approx(PFOF_PAYMENT_PER_SHARE * qty)

    def test_exchange_fee_saved_correct(self):
        """Exchange fee saved = EXCHANGE_FEE_PER_SHARE × qty."""
        qty = 500
        ats = _ats()
        trade = ats.route(_buy_order(qty=qty), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.INTERNALIZED
        assert trade.exchange_fee_saved == pytest.approx(EXCHANGE_FEE_PER_SHARE * qty)

    def test_net_benefit_positive_when_internalized(self):
        """Net benefit (fees_saved − pfof_paid) is positive after internalization."""
        ats = _ats()
        ats.route(_buy_order(qty=100), bid=_BID, ask=_ASK)
        s = ats.stats()
        assert s.net_benefit_usd > 0.0

    def test_summary_has_required_keys(self):
        """summary() returns all required keys."""
        ats = _ats()
        ats.route(_buy_order(), bid=_BID, ask=_ASK)
        result = ats.summary()
        required = {
            "total_orders", "internalized", "lit_routed",
            "internalization_rate", "total_pfof_paid_usd",
            "total_fees_saved_usd", "net_benefit_usd",
            "volume_internalized", "volume_lit",
        }
        assert required.issubset(result.keys())

    def test_stats_total_orders_increments(self):
        """stats().total_orders increments with each route() call."""
        ats = _ats()
        for i in range(1, 6):
            ats.route(_buy_order(), bid=_BID, ask=_ASK)
            assert ats.stats().total_orders == i

    def test_inventory_decreases_after_internalization(self):
        """long_qty is reduced by qty after a successful internalization."""
        ats = _ats()
        initial_long = ats.inventory("AAPL").long_qty
        qty = 300
        trade = ats.route(_buy_order(qty=qty), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.INTERNALIZED
        assert ats.inventory("AAPL").long_qty == initial_long - qty

    def test_lit_price_is_ask_for_buy_order(self):
        """Lit-routed buy order fills at the ask price."""
        ats = DarkPoolATS(min_spread_bps=1.0)   # no inventory → lit route
        trade = ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.LIT_MARKET
        assert trade.cross_price == pytest.approx(_ASK)

    def test_lit_price_is_bid_for_sell_order(self):
        """Lit-routed sell order fills at the bid price."""
        ats = DarkPoolATS(min_spread_bps=1.0)   # no inventory → lit route
        trade = ats.route(_sell_order(), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.LIT_MARKET
        assert trade.cross_price == pytest.approx(_BID)


# ─── TestPFoFRouting ──────────────────────────────────────────────────────────

class TestPFoFRouting:
    """Order flow classification and multi-symbol routing behaviour."""

    def test_retail_flow_eligible_for_internalization(self):
        """RETAIL flow is internalized when inventory and spread permit."""
        ats = _ats()
        trade = ats.route(_buy_order(flow_type=OrderFlow.RETAIL), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.INTERNALIZED

    def test_institutional_flow_can_be_internalized(self):
        """INSTITUTIONAL flow is not treated as arbitrage and can be internalized."""
        ats = _ats()
        order = _buy_order(flow_type=OrderFlow.INSTITUTIONAL)
        trade = ats.route(order, bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.INTERNALIZED

    def test_arbitrage_flow_always_lit(self):
        """ARBITRAGE flow is routed to lit market even with inventory and spread."""
        ats = _ats()
        order = IncomingOrder(
            order_id=uuid.uuid4().hex,
            symbol="AAPL",
            side="buy",
            qty=50,
            limit_price=150.05,
            flow_type=OrderFlow.ARBITRAGE,
        )
        trade = ats.route(order, bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.LIT_MARKET

    def test_multiple_symbols_independent_inventory(self):
        """Inventory for AAPL does not affect routing decisions for MSFT."""
        ats = DarkPoolATS(min_spread_bps=1.0)
        ats.update_inventory("AAPL", long_qty=5_000, short_qty=0, avg_cost=148.0)
        # No MSFT inventory → MSFT buy goes to lit market

        aapl_order = _buy_order(symbol="AAPL")
        msft_order = IncomingOrder(
            order_id=uuid.uuid4().hex,
            symbol="MSFT",
            side="buy",
            qty=100,
            limit_price=300.05,
            flow_type=OrderFlow.RETAIL,
        )
        aapl_trade = ats.route(aapl_order, bid=_BID, ask=_ASK)
        msft_trade = ats.route(msft_order, bid=300.00, ask=300.10)

        assert aapl_trade.routing_decision == RoutingDecision.INTERNALIZED
        assert msft_trade.routing_decision == RoutingDecision.LIT_MARKET

    def test_crossed_trade_has_trade_id(self):
        """Every CrossedTrade has a non-empty trade_id."""
        ats = _ats()
        trade = ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert isinstance(trade.trade_id, str)
        assert len(trade.trade_id) > 0

    def test_routing_decision_in_crossed_trade(self):
        """routing_decision field on CrossedTrade matches the actual routing."""
        ats = _ats()
        internalized = ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert internalized.routing_decision == RoutingDecision.INTERNALIZED

        ats_no_inv = DarkPoolATS(min_spread_bps=1.0)
        lit = ats_no_inv.route(_buy_order(), bid=_BID, ask=_ASK)
        assert lit.routing_decision == RoutingDecision.LIT_MARKET

    def test_pfof_zero_for_lit_routed_trades(self):
        """Lit-routed trades have zero PFoF payment and zero fee saved."""
        ats = DarkPoolATS(min_spread_bps=1.0)  # no inventory
        trade = ats.route(_buy_order(), bid=_BID, ask=_ASK)
        assert trade.routing_decision == RoutingDecision.LIT_MARKET
        assert trade.pfof_payment == 0.0
        assert trade.exchange_fee_saved == 0.0

    def test_mixed_routing_counts_correctly(self):
        """internalized + lit_routed counts sum to total_orders."""
        ats = _ats()
        # AAPL with inventory → internalized
        ats.route(_buy_order(symbol="AAPL"), bid=_BID, ask=_ASK)
        # MSFT with no inventory → lit
        ats.route(
            IncomingOrder(
                order_id=uuid.uuid4().hex,
                symbol="MSFT",
                side="buy",
                qty=100,
                limit_price=300.05,
                flow_type=OrderFlow.RETAIL,
            ),
            bid=300.00,
            ask=300.10,
        )
        s = ats.stats()
        assert s.internalized + s.lit_routed == s.total_orders
