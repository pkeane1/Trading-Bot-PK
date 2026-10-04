"""Tests for the Kalshi API client with mocked HTTP responses."""

from __future__ import annotations

import pytest
import httpx
import respx

from kalshi_bot.api.auth import KalshiAuth
from kalshi_bot.api.client import KalshiClient
from kalshi_bot.api.exceptions import KalshiAuthError, KalshiNotFoundError
from kalshi_bot.models import Side, OrderAction


# Use a fake auth that doesn't need real keys
class FakeAuth:
    """Test auth that returns dummy headers."""

    def sign_request(self, method: str, path: str) -> dict[str, str]:
        return {
            "KALSHI-ACCESS-KEY": "fake-key",
            "KALSHI-ACCESS-TIMESTAMP": "1234567890123",
            "KALSHI-ACCESS-SIGNATURE": "ZmFrZS1zaWduYXR1cmU=",
        }


BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


@pytest.fixture
def fake_client():
    """Create a client with fake auth for testing."""
    client = KalshiClient(
        base_url=BASE_URL,
        auth=FakeAuth(),  # type: ignore[arg-type]
    )
    return client


class TestGetBalance:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_balance(self, fake_client):
        respx.get(f"{BASE_URL}/portfolio/balance").mock(
            return_value=httpx.Response(200, json={"balance": 10000})
        )
        async with fake_client as client:
            balance = await client.get_balance()
            assert balance == 10000

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_balance_auth_error(self, fake_client):
        respx.get(f"{BASE_URL}/portfolio/balance").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        async with fake_client as client:
            with pytest.raises(KalshiAuthError):
                await client.get_balance()


class TestGetMarkets:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_markets_returns_list(self, fake_client):
        respx.get(f"{BASE_URL}/markets").mock(
            return_value=httpx.Response(
                200,
                json={
                    "markets": [
                        {
                            "ticker": "TEST-MKT",
                            "event_ticker": "TEST-EVT",
                            "title": "Test Market",
                            "status": "open",
                            "yes_ask": 60,
                            "yes_bid": 55,
                            "volume": 1000,
                            "open_interest": 500,
                        }
                    ],
                    "cursor": None,
                },
            )
        )
        async with fake_client as client:
            markets, cursor = await client.get_markets()
            assert len(markets) == 1
            assert markets[0].ticker == "TEST-MKT"
            assert markets[0].yes_ask == 60
            assert cursor is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_markets_pagination(self, fake_client):
        respx.get(f"{BASE_URL}/markets").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "markets": [{"ticker": "MKT-1", "event_ticker": "EVT", "title": "M1", "status": "open"}],
                        "cursor": "page2",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "markets": [{"ticker": "MKT-2", "event_ticker": "EVT", "title": "M2", "status": "open"}],
                        "cursor": None,
                    },
                ),
            ]
        )
        async with fake_client as client:
            all_markets = await client.get_all_markets()
            assert len(all_markets) == 2
            assert all_markets[0].ticker == "MKT-1"
            assert all_markets[1].ticker == "MKT-2"


class TestGetMarket:
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_single_market(self, fake_client):
        respx.get(f"{BASE_URL}/markets/TEST-TICKER").mock(
            return_value=httpx.Response(
                200,
                json={"market": {"ticker": "TEST-TICKER", "event_ticker": "EVT", "title": "Test", "status": "open", "last_price": 42}},
            )
        )
        async with fake_client as client:
            market = await client.get_market("TEST-TICKER")
            assert market.ticker == "TEST-TICKER"
            assert market.last_price == 42

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_market_not_found(self, fake_client):
        respx.get(f"{BASE_URL}/markets/NOPE").mock(
            return_value=httpx.Response(404, text="Not found")
        )
        async with fake_client as client:
            with pytest.raises(KalshiNotFoundError):
                await client.get_market("NOPE")


class TestCreateOrder:
    @respx.mock
    @pytest.mark.asyncio
    async def test_create_order(self, fake_client):
        respx.post(f"{BASE_URL}/portfolio/orders").mock(
            return_value=httpx.Response(
                200,
                json={
                    "order": {
                        "order_id": "ord-123",
                        "ticker": "TEST-MKT",
                        "side": "yes",
                        "action": "buy",
                        "type": "limit",
                        "count": 5,
                        "yes_price": 45,
                        "status": "resting",
                        "remaining_count": 5,
                    }
                },
            )
        )
        async with fake_client as client:
            order = await client.create_order(
                ticker="TEST-MKT",
                side=Side.YES,
                action=OrderAction.BUY,
                count=5,
                price_cents=45,
            )
            assert order.order_id == "ord-123"
            assert order.quantity == 5
            assert order.price_cents == 45
