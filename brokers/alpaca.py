"""
Alpaca broker implementation (paper + live).

Uses alpaca-py SDK. Paper trading is the default and is required for
shadow mode per §8.2. Live trading requires ALPACA_BASE_URL to point
to the live endpoint and Human Governance sign-off.
"""
from __future__ import annotations

import logging
from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide as AlpacaSide,
    TimeInForce as AlpacaTIF,
    QueryOrderStatus,
)

from brokers.base import AccountInfo, BrokerBase, BrokerOrder, BrokerPosition
from core.enums import OrderSide, TimeInForce
from core.exceptions import BrokerAPIError, BrokerConnectionError

logger = logging.getLogger(__name__)


def _map_side(side: OrderSide) -> AlpacaSide:
    return AlpacaSide.BUY if side in (OrderSide.BUY, OrderSide.BUY_TO_COVER) else AlpacaSide.SELL


def _map_tif(tif: TimeInForce) -> AlpacaTIF:
    mapping = {
        TimeInForce.DAY: AlpacaTIF.DAY,
        TimeInForce.GTC: AlpacaTIF.GTC,
        TimeInForce.IOC: AlpacaTIF.IOC,
        TimeInForce.FOK: AlpacaTIF.FOK,
    }
    return mapping.get(tif, AlpacaTIF.DAY)


def _broker_order_from_alpaca(ao) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=str(ao.id),
        client_order_id=str(ao.client_order_id) if ao.client_order_id else "",
        symbol=ao.symbol,
        side=ao.side.value if ao.side else "buy",
        qty=int(ao.qty or 0),
        order_type=ao.order_type.value if ao.order_type else "limit",
        status=ao.status.value if ao.status else "pending_new",
        filled_qty=int(ao.filled_qty or 0),
        avg_fill_price=float(ao.filled_avg_price) if ao.filled_avg_price else None,
        limit_price=float(ao.limit_price) if ao.limit_price else None,
        stop_price=float(ao.stop_price) if ao.stop_price else None,
        submitted_at=ao.submitted_at,
        filled_at=ao.filled_at,
    )


class AlpacaBroker(BrokerBase):
    """
    Alpaca paper/live trading broker.

    Initialization:
        broker = AlpacaBroker(api_key, secret_key, paper=True)
        await broker.connect()
    """

    def __init__(self, api_key: str, secret_key: str, paper: bool = True) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._client: TradingClient | None = None

    async def connect(self) -> None:
        try:
            self._client = TradingClient(
                api_key=self._api_key,
                secret_key=self._secret_key,
                paper=self._paper,
            )
            # Validate credentials immediately
            await self.get_account()
            logger.info(
                "AlpacaBroker connected (%s)", "paper" if self._paper else "LIVE"
            )
        except Exception as exc:
            raise BrokerConnectionError(f"Alpaca connection failed: {exc}") from exc

    async def disconnect(self) -> None:
        self._client = None
        logger.info("AlpacaBroker disconnected")

    async def is_connected(self) -> bool:
        try:
            if not self._client:
                return False
            await self.get_account()
            return True
        except Exception:
            return False

    def _require_client(self) -> TradingClient:
        if not self._client:
            raise BrokerConnectionError("Alpaca client not connected")
        return self._client

    async def get_account(self) -> AccountInfo:
        client = self._require_client()
        try:
            acct = client.get_account()
            return AccountInfo(
                account_id=str(acct.id),
                equity=float(acct.equity or 0),
                buying_power=float(acct.buying_power or 0),
                cash=float(acct.cash or 0),
                day_trade_buying_power=float(acct.daytrading_buying_power or 0),
                pattern_day_trader=bool(acct.pattern_day_trader),
                day_trades_remaining=int(acct.daytrade_count or 0),
            )
        except Exception as exc:
            raise BrokerAPIError(500, str(exc)) from exc

    async def submit_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        limit_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
        client_order_id: str | None = None,
    ) -> BrokerOrder:
        client = self._require_client()
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=_map_side(side),
            limit_price=limit_price,
            time_in_force=_map_tif(time_in_force),
            client_order_id=client_order_id,
        )
        try:
            order = client.submit_order(req)
            logger.info(
                "Limit order submitted: %s %s x%d @ %.2f", side.value, symbol, qty, limit_price
            )
            return _broker_order_from_alpaca(order)
        except Exception as exc:
            raise BrokerAPIError(500, str(exc)) from exc

    async def submit_bracket_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        limit_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        time_in_force: TimeInForce = TimeInForce.DAY,
        client_order_id: str | None = None,
    ) -> BrokerOrder:
        client = self._require_client()
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=_map_side(side),
            limit_price=limit_price,
            time_in_force=_map_tif(time_in_force),
            client_order_id=client_order_id,
            order_class="bracket",
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_loss_price),
        )
        try:
            order = client.submit_order(req)
            logger.info(
                "Bracket order submitted: %s %s x%d entry=%.2f stop=%.2f target=%.2f",
                side.value, symbol, qty, limit_price, stop_loss_price, take_profit_price,
            )
            return _broker_order_from_alpaca(order)
        except Exception as exc:
            raise BrokerAPIError(500, str(exc)) from exc

    async def submit_market_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        client_order_id: str | None = None,
    ) -> BrokerOrder:
        client = self._require_client()
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=_map_side(side),
            time_in_force=AlpacaTIF.DAY,
            client_order_id=client_order_id,
        )
        try:
            order = client.submit_order(req)
            logger.warning(
                "MARKET order submitted (emergency): %s %s x%d", side.value, symbol, qty
            )
            return _broker_order_from_alpaca(order)
        except Exception as exc:
            raise BrokerAPIError(500, str(exc)) from exc

    async def cancel_order(self, broker_order_id: str) -> bool:
        client = self._require_client()
        try:
            client.cancel_order_by_id(broker_order_id)
            return True
        except Exception as exc:
            logger.error("Cancel order %s failed: %s", broker_order_id, exc)
            return False

    async def cancel_all_orders(self) -> int:
        client = self._require_client()
        try:
            cancelled = client.cancel_orders()
            count = len(cancelled) if cancelled else 0
            logger.warning("Cancelled %d open orders", count)
            return count
        except Exception as exc:
            logger.error("cancel_all_orders failed: %s", exc)
            return 0

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        client = self._require_client()
        try:
            order = client.get_order_by_id(broker_order_id)
            return _broker_order_from_alpaca(order)
        except Exception:
            return None

    async def get_open_orders(self) -> list[BrokerOrder]:
        client = self._require_client()
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = client.get_orders(req)
            return [_broker_order_from_alpaca(o) for o in orders]
        except Exception as exc:
            logger.error("get_open_orders failed: %s", exc)
            return []

    async def get_positions(self) -> list[BrokerPosition]:
        client = self._require_client()
        try:
            positions = client.get_all_positions()
            result = []
            for p in positions:
                result.append(BrokerPosition(
                    symbol=p.symbol,
                    qty=int(p.qty or 0),
                    avg_entry_price=float(p.avg_entry_price or 0),
                    current_price=float(p.current_price or 0),
                    unrealized_pnl=float(p.unrealized_pl or 0),
                    side=p.side.value if p.side else "long",
                ))
            return result
        except Exception as exc:
            logger.error("get_positions failed: %s", exc)
            return []

    async def close_position(self, symbol: str) -> BrokerOrder | None:
        client = self._require_client()
        try:
            order = client.close_position(symbol)
            logger.warning("EMERGENCY: Closed position for %s", symbol)
            return _broker_order_from_alpaca(order)
        except Exception as exc:
            logger.error("close_position(%s) failed: %s", symbol, exc)
            return None

    async def close_all_positions(self) -> list[BrokerOrder]:
        client = self._require_client()
        try:
            result = client.close_all_positions(cancel_orders=True)
            orders = []
            if result:
                for item in result:
                    if hasattr(item, 'body') and item.body:
                        orders.append(_broker_order_from_alpaca(item.body))
            logger.warning("EMERGENCY: Flattened all positions (%d)", len(orders))
            return orders
        except Exception as exc:
            logger.error("close_all_positions failed: %s", exc)
            return []

    async def is_market_open(self) -> bool:
        try:
            clock = self._require_client().get_clock()
            return bool(clock.is_open)
        except Exception:
            return False

    async def get_clock(self) -> dict:
        try:
            clock = self._require_client().get_clock()
            return {
                "is_open": clock.is_open,
                "next_open": clock.next_open.isoformat() if clock.next_open else None,
                "next_close": clock.next_close.isoformat() if clock.next_close else None,
                "timestamp": clock.timestamp.isoformat() if clock.timestamp else None,
            }
        except Exception as exc:
            raise BrokerAPIError(500, str(exc)) from exc
