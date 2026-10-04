"""Real-time WebSocket client for Kalshi price and fill streaming."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Coroutine

import structlog
import websockets
from websockets.asyncio.client import ClientConnection

from kalshi_bot.api.auth import KalshiAuth

logger = structlog.get_logger(__name__)

MessageHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class KalshiWebSocket:
    """WebSocket client for real-time Kalshi market data.

    Supports subscribing to:
    - Orderbook updates
    - Ticker price updates
    - Fill notifications
    """

    def __init__(self, ws_url: str, auth: KalshiAuth) -> None:
        self.ws_url = ws_url
        self.auth = auth
        self._connection: ClientConnection | None = None
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._running = False
        self._subscriptions: list[dict[str, Any]] = []

    def on(self, channel: str, handler: MessageHandler) -> None:
        """Register a handler for a message channel."""
        self._handlers.setdefault(channel, []).append(handler)

    async def connect(self) -> None:
        """Connect and authenticate to the WebSocket."""
        auth_headers = self.auth.sign_request("GET", "/trade-api/ws/v2")
        headers = {
            "KALSHI-ACCESS-KEY": auth_headers["KALSHI-ACCESS-KEY"],
            "KALSHI-ACCESS-TIMESTAMP": auth_headers["KALSHI-ACCESS-TIMESTAMP"],
            "KALSHI-ACCESS-SIGNATURE": auth_headers["KALSHI-ACCESS-SIGNATURE"],
        }
        self._connection = await websockets.connect(
            self.ws_url,
            additional_headers=headers,
        )
        self._running = True
        logger.info("websocket_connected", url=self.ws_url)

    async def subscribe_orderbook(self, tickers: list[str]) -> None:
        """Subscribe to orderbook updates for given tickers."""
        msg = {
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": tickers},
        }
        self._subscriptions.append(msg)
        if self._connection:
            await self._connection.send(json.dumps(msg))
            logger.info("ws_subscribed_orderbook", tickers=tickers)

    async def subscribe_ticker(self, tickers: list[str]) -> None:
        """Subscribe to ticker price updates."""
        msg = {
            "id": 2,
            "cmd": "subscribe",
            "params": {"channels": ["ticker"], "market_tickers": tickers},
        }
        self._subscriptions.append(msg)
        if self._connection:
            await self._connection.send(json.dumps(msg))
            logger.info("ws_subscribed_ticker", tickers=tickers)

    async def subscribe_fills(self) -> None:
        """Subscribe to fill notifications."""
        msg = {
            "id": 3,
            "cmd": "subscribe",
            "params": {"channels": ["fill"]},
        }
        self._subscriptions.append(msg)
        if self._connection:
            await self._connection.send(json.dumps(msg))
            logger.info("ws_subscribed_fills")

    async def listen(self) -> None:
        """Main listen loop — dispatches messages to registered handlers."""
        if not self._connection:
            raise RuntimeError("Not connected — call connect() first")

        try:
            async for raw_message in self._connection:
                if not self._running:
                    break
                try:
                    message = json.loads(raw_message)
                    channel = message.get("type", message.get("channel", ""))
                    handlers = self._handlers.get(channel, [])
                    for handler in handlers:
                        await handler(message)
                except json.JSONDecodeError:
                    logger.warning("ws_invalid_json", data=str(raw_message)[:200])
                except Exception as e:
                    logger.error("ws_handler_error", error=str(e))
        except websockets.ConnectionClosed as e:
            logger.warning("ws_connection_closed", code=e.code, reason=e.reason)
        except Exception as e:
            logger.error("ws_listen_error", error=str(e))

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._running = False
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("websocket_closed")
