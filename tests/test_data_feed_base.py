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
