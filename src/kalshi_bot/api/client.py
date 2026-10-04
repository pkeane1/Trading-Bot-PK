"""Async Kalshi REST API client with auth, rate limiting, and retries."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from kalshi_bot.api.auth import KalshiAuth
from kalshi_bot.api.exceptions import KalshiRateLimitError, KalshiServiceUnavailableError, raise_for_status
from kalshi_bot.api.rate_limiter import DualRateLimiter
from kalshi_bot.models import (
    Event,
    Fill,
    Market,
    Order,
    OrderBook,
    OrderBookLevel,
    Side,
    OrderAction,
    OrderType,
)

logger = structlog.get_logger(__name__)

# HTTP methods that are "write" operations
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class KalshiClient:
    """Async Kalshi REST API client.

    Usage:
        async with KalshiClient(base_url, auth) as client:
            balance = await client.get_balance()
    """

    def __init__(
        self,
        base_url: str,
        auth: KalshiAuth,
        rate_limiter: DualRateLimiter | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.rate_limiter = rate_limiter or DualRateLimiter()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> KalshiClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Core request method ─────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type((KalshiRateLimitError, KalshiServiceUnavailableError, httpx.TransportError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=1, max=30),
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated, rate-limited request to the Kalshi API."""
        assert self._client is not None, "Client not initialized — use 'async with'"

        # Rate limit
        if method.upper() in _WRITE_METHODS:
            await self.rate_limiter.acquire_write()
        else:
            await self.rate_limiter.acquire_read()

        # Build auth headers — sign the path without query string
        parsed = urlparse(path)
        sign_path = f"/trade-api/v2{parsed.path}" if not path.startswith("/trade-api") else parsed.path
        auth_headers = self.auth.sign_request(method.upper(), sign_path)

        response = await self._client.request(
            method, path, params=params, json=json, headers=auth_headers
        )

        body = response.text
        if response.status_code >= 400:
            logger.warning(
                "api_error_response",
                status=response.status_code,
                method=method,
                path=path,
                body=body[:500],
            )
        raise_for_status(response.status_code, body)

        if not body:
            return {}
        return response.json()  # type: ignore[no-any-return]

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("POST", path, json=json)

    async def _delete(self, path: str) -> dict[str, Any]:
        return await self._request("DELETE", path)

    # ── Market endpoints ────────────────────────────────────────────────────

    async def get_markets(
        self,
        cursor: str | None = None,
        limit: int = 200,
        status: str = "open",
        event_ticker: str | None = None,
    ) -> tuple[list[Market], str | None]:
        """Fetch a page of markets. Returns (markets, next_cursor)."""
        params: dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker

        data = await self._get("/markets", params=params)
        markets_raw = data.get("markets", [])
        next_cursor = data.get("cursor")

        markets = [_parse_market(m) for m in markets_raw]
        return markets, next_cursor if next_cursor else None

    async def get_all_markets(self, status: str = "open", **kwargs: Any) -> list[Market]:
        """Fetch all markets, auto-following pagination cursors."""
        all_markets: list[Market] = []
        cursor: str | None = None
        while True:
            markets, cursor = await self.get_markets(cursor=cursor, status=status, **kwargs)
            all_markets.extend(markets)
            if not cursor:
                break
        logger.info("fetched_all_markets", count=len(all_markets))
        return all_markets

    async def get_market(self, ticker: str) -> Market:
        """Fetch a single market by ticker."""
        data = await self._get(f"/markets/{ticker}")
        return _parse_market(data.get("market", data))

    async def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook:
        """Fetch the order book for a market."""
        data = await self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        ob = data.get("orderbook", data)
        return OrderBook(
            ticker=ticker,
            yes_bids=[OrderBookLevel(price=lvl[0], quantity=lvl[1]) for lvl in ob.get("yes", []) if len(lvl) >= 2] if isinstance(ob.get("yes"), list) else [],
            yes_asks=[],
            no_bids=[],
            no_asks=[],
        )

    # ── Event endpoints ─────────────────────────────────────────────────────

    async def get_event(self, event_ticker: str) -> Event:
        """Fetch an event and its markets."""
        data = await self._get(f"/events/{event_ticker}")
        event_data = data.get("event", data)
        markets_raw = event_data.get("markets", [])
        return Event(
            event_ticker=event_data.get("event_ticker", event_ticker),
            title=event_data.get("title", ""),
            subtitle=event_data.get("subtitle", ""),
            category=event_data.get("category", ""),
            mutually_exclusive=event_data.get("mutually_exclusive", False),
            markets=[_parse_market(m) for m in markets_raw],
        )

    async def get_events(
        self,
        cursor: str | None = None,
        limit: int = 100,
        status: str = "open",
    ) -> tuple[list[Event], str | None]:
        """Fetch a page of events."""
        params: dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        data = await self._get("/events", params=params)
        events_raw = data.get("events", [])
        next_cursor = data.get("cursor")
        events = [
            Event(
                event_ticker=e.get("event_ticker", ""),
                title=e.get("title", ""),
                subtitle=e.get("subtitle", ""),
                category=e.get("category", ""),
                mutually_exclusive=e.get("mutually_exclusive", False),
            )
            for e in events_raw
        ]
        return events, next_cursor if next_cursor else None

    # ── Portfolio endpoints ─────────────────────────────────────────────────

    async def get_balance(self) -> int:
        """Get account balance in cents."""
        data = await self._get("/portfolio/balance")
        return int(data.get("balance", 0))

    async def get_positions(self) -> list[dict[str, Any]]:
        """Get current positions."""
        data = await self._get("/portfolio/positions")
        return data.get("market_positions", [])

    async def get_fills(
        self,
        ticker: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> tuple[list[Fill], str | None]:
        """Get trade fills."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor

        data = await self._get("/portfolio/fills", params=params)
        fills_raw = data.get("fills", [])
        next_cursor = data.get("cursor")

        fills = [
            Fill(
                trade_id=f.get("trade_id", ""),
                ticker=f.get("ticker", ""),
                side=Side(f["side"]) if f.get("side") else Side.YES,
                action=OrderAction(f["action"]) if f.get("action") else OrderAction.BUY,
                count=int(f.get("count", 0)),
                price_cents=int(f.get("yes_price", 0)),
            )
            for f in fills_raw
        ]
        return fills, next_cursor if next_cursor else None

    # ── Order endpoints ─────────────────────────────────────────────────────

    async def create_order(
        self,
        ticker: str,
        side: Side,
        action: OrderAction,
        count: int,
        price_cents: int,
        order_type: OrderType = OrderType.LIMIT,
        client_order_id: str | None = None,
    ) -> Order:
        """Place a single order."""
        if client_order_id is None:
            client_order_id = str(uuid.uuid4())

        payload: dict[str, Any] = {
            "ticker": ticker,
            "side": side.value,
            "action": action.value,
            "count": count,
            "type": order_type.value,
            "client_order_id": client_order_id,
        }
        if order_type == OrderType.LIMIT:
            # Kalshi expects yes_price or no_price
            if side == Side.YES:
                payload["yes_price"] = price_cents
            else:
                payload["no_price"] = price_cents

        data = await self._post("/portfolio/orders", json=payload)
        order_data = data.get("order", data)
        return _parse_order(order_data)

    async def batch_create_orders(
        self, orders: list[dict[str, Any]]
    ) -> list[Order]:
        """Place up to 20 orders in a single batch."""
        data = await self._post("/portfolio/orders/batched", json={"orders": orders[:20]})
        return [_parse_order(o) for o in data.get("orders", [])]

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order."""
        await self._delete(f"/portfolio/orders/{order_id}")
        logger.info("order_cancelled", order_id=order_id)

    async def get_orders(
        self,
        ticker: str | None = None,
        status: str | None = None,
    ) -> list[Order]:
        """Get orders, optionally filtered."""
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = await self._get("/portfolio/orders", params=params)
        return [_parse_order(o) for o in data.get("orders", [])]


# ── Parsing helpers ─────────────────────────────────────────────────────────────


def _parse_market(m: dict[str, Any]) -> Market:
    return Market(
        ticker=m.get("ticker", ""),
        event_ticker=m.get("event_ticker", ""),
        title=m.get("title", ""),
        subtitle=m.get("subtitle", ""),
        status=m.get("status", ""),
        yes_ask=int(m.get("yes_ask", 0) or 0),
        yes_bid=int(m.get("yes_bid", 0) or 0),
        no_ask=int(m.get("no_ask", 0) or 0),
        no_bid=int(m.get("no_bid", 0) or 0),
        last_price=int(m.get("last_price", 0) or 0),
        volume=int(m.get("volume", 0) or 0),
        open_interest=int(m.get("open_interest", 0) or 0),
        close_time=m.get("close_time"),
        category=m.get("category", ""),
        result=m.get("result", ""),
    )


def _parse_order(o: dict[str, Any]) -> Order:
    return Order(
        order_id=o.get("order_id", ""),
        client_order_id=o.get("client_order_id", ""),
        ticker=o.get("ticker", ""),
        side=Side(o["side"]) if o.get("side") else Side.YES,
        action=OrderAction(o["action"]) if o.get("action") else OrderAction.BUY,
        type=OrderType(o["type"]) if o.get("type") else OrderType.LIMIT,
        quantity=int(o.get("count", 0) or o.get("quantity", 0)),
        price_cents=int(o.get("yes_price", 0) or o.get("no_price", 0) or 0),
        status=o.get("status", "pending"),
        remaining_count=int(o.get("remaining_count", 0)),
    )
