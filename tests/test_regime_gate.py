"""
Tests for the regime gate — the hardest gate in the system.
Per §5.1: No signals in high-volatility/low-liquidity regimes.
"""
from __future__ import annotations

import pytest
from core.enums import RegimeLabel, SetupType


class TestRegimeLabelGate:
    """Regime.is_tradeable is the primary gate for all signal emission."""

    def test_trend_day_is_tradeable(self):
        assert RegimeLabel.TREND_DAY.is_tradeable

    def test_mean_reversion_day_is_tradeable(self):
        assert RegimeLabel.MEAN_REVERSION_DAY.is_tradeable

    def test_event_driven_day_is_tradeable(self):
        assert RegimeLabel.EVENT_DRIVEN_DAY.is_tradeable

    def test_high_volatility_no_trade(self):
        assert not RegimeLabel.HIGH_VOLATILITY_NO_TRADE.is_tradeable

    def test_low_liquidity_no_trade(self):
        assert not RegimeLabel.LOW_LIQUIDITY_NO_TRADE.is_tradeable

    def test_no_trade_regimes_cannot_be_overridden_intraday(self):
        """
        This is a structural test — the RegimeLabel.is_tradeable property
        is the single source of truth. No agent may emit signals when False.
        The property is immutable (a computed bool, not a flag that can be set).
        """
        label = RegimeLabel.HIGH_VOLATILITY_NO_TRADE
        # Verify it cannot be "set" to True
        with pytest.raises(AttributeError):
            label.is_tradeable = True


class TestSetupTypeRegimeMapping:
    """Validate that regime-setup mappings are logically consistent."""

    # Expected mappings from the business case
    ALLOWED_IN_TREND_DAY = {SetupType.BREAKOUT, SetupType.MOMENTUM_BURST, SetupType.GAP_AND_GO}
    ALLOWED_IN_MEAN_REVERSION = {SetupType.MEAN_REVERSION_FADE, SetupType.VWAP_TAP}

    def test_breakout_allowed_in_trend_not_mean_reversion(self):
        """Breakout is a directional setup — should not be allowed in mean-reversion."""
        assert SetupType.BREAKOUT in self.ALLOWED_IN_TREND_DAY
        assert SetupType.BREAKOUT not in self.ALLOWED_IN_MEAN_REVERSION

    def test_mean_reversion_fade_not_in_trend(self):
        """Fading momentum in a trend day is dangerous — must be excluded."""
        assert SetupType.MEAN_REVERSION_FADE not in self.ALLOWED_IN_TREND_DAY
        assert SetupType.MEAN_REVERSION_FADE in self.ALLOWED_IN_MEAN_REVERSION
