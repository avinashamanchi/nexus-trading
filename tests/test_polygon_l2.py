"""Unit tests for PolygonL2Manager. No network required — HTTP is mocked."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.level2 import OrderBookCache


@pytest.fixture
def order_book():
    return OrderBookCache()


@pytest.fixture
def l2_manager(order_book):
    from data.polygon_l2 import PolygonL2Manager
    return PolygonL2Manager(api_key="test_key", order_book=order_book, poll_sec=2)


def test_l2_manager_instantiates(l2_manager):
    assert l2_manager is not None


@pytest.mark.asyncio
async def test_nbbo_quote_updates_order_book_level_zero(l2_manager, order_book):
    """A Q.* style NBBO update should set level [0] bid and ask."""
    await l2_manager.update_from_nbbo("AAPL", bid=150.49, bid_size=500, ask=150.51, ask_size=300)

    book = order_book.get_book("AAPL")
    assert book is not None
    assert len(book.bids) >= 1
    assert len(book.asks) >= 1
    assert book.bids[0].price == 150.49
    assert book.bids[0].size == 500
    assert book.asks[0].price == 150.51
    assert book.asks[0].size == 300


@pytest.mark.asyncio
async def test_rest_snapshot_enriches_order_book(l2_manager, order_book):
    """A parsed REST snapshot response should enrich OrderBookCache."""
    snapshot_data = {
        "ticker": {
            "lastQuote": {"P": 150.51, "S": 300, "p": 150.49, "s": 500, "t": 1623081600000},
            "day": {"vw": 150.55, "v": 500000},
            "min": {"o": 150.49, "c": 150.52, "h": 150.57, "l": 150.45, "v": 1000, "vw": 150.55},
        }
    }
    await l2_manager._apply_snapshot("AAPL", snapshot_data)

    book = order_book.get_book("AAPL")
    assert book is not None
    assert book.bids[0].price == 150.49
    assert book.asks[0].price == 150.51


@pytest.mark.asyncio
async def test_rest_snapshot_http_called(l2_manager):
    """poll_once() should make an HTTP GET to Polygon REST API."""
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "ticker": {
            "lastQuote": {"P": 150.51, "S": 100, "p": 150.49, "s": 200, "t": 1000},
            "day": {"vw": 150.5, "v": 10000},
            "min": {"o": 150.0, "c": 150.5, "h": 151.0, "l": 149.5, "v": 1000, "vw": 150.4},
        }
    })

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("aiohttp.ClientSession", return_value=mock_session):
        await l2_manager.poll_once(["AAPL"])

    mock_session.get.assert_called_once()
    call_url = mock_session.get.call_args[0][0]
    assert "AAPL" in call_url
    assert "polygon.io" in call_url


@pytest.mark.asyncio
async def test_snapshot_missing_quote_does_not_crash(l2_manager):
    """Snapshot response with missing lastQuote should be handled gracefully."""
    snapshot_data = {"ticker": {"day": {"vw": 150.5, "v": 10000}}}
    # Should not raise
    await l2_manager._apply_snapshot("AAPL", snapshot_data)


@pytest.mark.asyncio
async def test_stop_cancels_poll_task(l2_manager):
    """Calling stop() should cancel the background poll loop."""
    # run() accepts a callable that returns the current symbol list
    task = asyncio.create_task(l2_manager.run(lambda: ["AAPL"]))
    await asyncio.sleep(0.05)
    await l2_manager.stop()
    await asyncio.sleep(0.05)
    assert task.done() or task.cancelled()
