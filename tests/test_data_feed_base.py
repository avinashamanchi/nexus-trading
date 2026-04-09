# tests/test_data_feed_base.py
import pytest
from data.base import DataFeedBase
from core.exceptions import FeedConnectionError


def test_feed_connection_error_is_trading_system_error():
    from core.exceptions import TradingSystemError
    err = FeedConnectionError("bad key")
    assert isinstance(err, TradingSystemError)
    assert "bad key" in str(err)


def test_data_feed_base_is_abstract():
    """DataFeedBase cannot be instantiated directly."""
    with pytest.raises(TypeError):
        DataFeedBase()


def test_data_feed_base_required_methods():
    """Concrete subclass must implement all abstract methods."""
    from data.base import DataFeedBase
    import inspect
    abstract = {
        name for name, val in inspect.getmembers(DataFeedBase)
        if getattr(val, "__isabstractmethod__", False)
    }
    expected = {
        "add_tick_handler", "add_bar_handler",
        "subscribe", "unsubscribe",
        "get_latest_tick", "get_tick_history", "get_bar_history",
        "run", "stop", "is_running", "subscribed_symbols",
    }
    assert expected.issubset(abstract)


def test_alpaca_feed_is_data_feed_base():
    from data.feed import AlpacaDataFeed
    from data.base import DataFeedBase
    feed = AlpacaDataFeed(api_key="k", secret_key="s")
    assert isinstance(feed, DataFeedBase)


def test_polygon_feed_is_data_feed_base():
    from data.polygon_feed import PolygonDataFeed
    from data.base import DataFeedBase
    feed = PolygonDataFeed(api_key="k", use_delayed=True)
    assert isinstance(feed, DataFeedBase)


def test_both_feeds_expose_identical_method_signatures():
    """Both feeds must have the same set of public DataFeedBase methods."""
    import inspect
    from data.feed import AlpacaDataFeed
    from data.polygon_feed import PolygonDataFeed
    from data.base import DataFeedBase

    base_methods = {
        name for name, val in inspect.getmembers(DataFeedBase)
        if not name.startswith("_") and callable(val)
    }

    alpaca_methods = {
        name for name, val in inspect.getmembers(AlpacaDataFeed)
        if not name.startswith("_") and callable(val)
    }
    polygon_methods = {
        name for name, val in inspect.getmembers(PolygonDataFeed)
        if not name.startswith("_") and callable(val)
    }

    missing_alpaca = base_methods - alpaca_methods
    missing_polygon = base_methods - polygon_methods

    assert not missing_alpaca, f"AlpacaDataFeed missing: {missing_alpaca}"
    assert not missing_polygon, f"PolygonDataFeed missing: {missing_polygon}"


@pytest.mark.asyncio
async def test_polygon_feed_get_recent_bars_delegates_to_get_bar_history():
    from data.polygon_feed import PolygonDataFeed
    feed = PolygonDataFeed(api_key="k", use_delayed=True)
    # No bars yet — both should return empty list
    assert feed.get_recent_bars("AAPL", timeframe="1m", limit=5) == []
    assert feed.get_bar_history("AAPL", n=5) == []


@pytest.mark.asyncio
async def test_alpaca_feed_get_recent_bars_delegates_to_get_bar_history():
    from data.feed import AlpacaDataFeed
    feed = AlpacaDataFeed(api_key="k", secret_key="s")
    assert feed.get_recent_bars("AAPL", timeframe="1m", limit=5) == []
    assert feed.get_bar_history("AAPL", n=5) == []
