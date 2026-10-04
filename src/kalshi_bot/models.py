"""Shared Pydantic data models used across the trading bot."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────────


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    RESTING = "resting"
    CANCELED = "canceled"
    EXECUTED = "executed"
    PENDING = "pending"


# ── Market Data Models ─────────────────────────────────────────────────────────


class Market(BaseModel):
    """A single Kalshi market (contract)."""

    ticker: str
    event_ticker: str
    title: str
    subtitle: str = ""
    status: str
    yes_ask: int = 0  # cents
    yes_bid: int = 0  # cents
    no_ask: int = 0  # cents
    no_bid: int = 0  # cents
    last_price: int = 0  # cents
    volume: int = 0
    open_interest: int = 0
    close_time: datetime | None = None
    category: str = ""
    result: str = ""

    @property
    def midpoint(self) -> int:
        """Midpoint price in cents, or last_price as fallback."""
        if self.yes_bid > 0 and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) // 2
        return self.last_price

    @property
    def spread(self) -> int:
        """Bid-ask spread in cents."""
        if self.yes_bid > 0 and self.yes_ask > 0:
            return self.yes_ask - self.yes_bid
        return 0


class Event(BaseModel):
    """A Kalshi event (group of related markets)."""

    event_ticker: str
    title: str
    subtitle: str = ""
    category: str = ""
    mutually_exclusive: bool = False
    markets: list[Market] = Field(default_factory=list)


class OrderBookLevel(BaseModel):
    price: int  # cents
    quantity: int


class OrderBook(BaseModel):
    ticker: str
    yes_bids: list[OrderBookLevel] = Field(default_factory=list)
    yes_asks: list[OrderBookLevel] = Field(default_factory=list)
    no_bids: list[OrderBookLevel] = Field(default_factory=list)
    no_asks: list[OrderBookLevel] = Field(default_factory=list)


# ── Analysis Models ────────────────────────────────────────────────────────────


class MarketAnalysis(BaseModel):
    """Claude's analysis of a single market — also used as structured output schema."""

    ticker: str
    predicted_probability: float = Field(
        ge=0.0, le=1.0, description="Estimated probability the YES outcome occurs (0.0-1.0)"
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="How confident the model is in its estimate (0.0-1.0)"
    )
    reasoning: str = Field(description="Brief explanation of the probability estimate")
    key_factors: list[str] = Field(
        default_factory=list, description="Key factors driving the estimate"
    )
    suggested_side: Side | None = Field(
        default=None, description="Which side looks favorable, if any"
    )


class EventAnalysis(BaseModel):
    """Claude's analysis of an entire event with all its markets."""

    event_ticker: str
    event_summary: str = Field(description="Brief summary of the event context")
    market_analyses: list[MarketAnalysis] = Field(default_factory=list)
    correlations: str = Field(
        default="", description="Notable correlations between markets in this event"
    )


# ── Trading Decision Models ───────────────────────────────────────────────────


class TradeDecision(BaseModel):
    """A decision to place a trade, output by the strategy engine."""

    ticker: str
    side: Side
    action: OrderAction = OrderAction.BUY
    quantity: int = Field(ge=1, description="Number of contracts")
    limit_price_cents: int = Field(ge=1, le=99, description="Limit price in cents")
    edge: float = Field(description="Expected edge as a fraction (e.g. 0.08 = 8%)")
    predicted_probability: float
    market_price_cents: int
    confidence: float
    reasoning: str


# ── Portfolio Models ───────────────────────────────────────────────────────────


class Position(BaseModel):
    """A current position on Kalshi."""

    ticker: str
    side: Side
    quantity: int
    average_price_cents: int  # average entry price in cents
    market_price_cents: int = 0  # current market price in cents
    entry_time: datetime | None = None

    @property
    def cost_cents(self) -> int:
        return self.quantity * self.average_price_cents

    @property
    def market_value_cents(self) -> int:
        return self.quantity * self.market_price_cents

    @property
    def unrealized_pnl_cents(self) -> int:
        return self.market_value_cents - self.cost_cents


class Fill(BaseModel):
    """A trade fill from Kalshi."""

    trade_id: str = ""
    ticker: str
    side: Side
    action: OrderAction
    count: int
    price_cents: int  # price per contract in cents
    created_time: datetime | None = None


class Order(BaseModel):
    """An order on Kalshi."""

    order_id: str
    client_order_id: str = ""
    ticker: str
    side: Side
    action: OrderAction
    type: OrderType = OrderType.LIMIT
    quantity: int
    price_cents: int
    status: OrderStatus = OrderStatus.PENDING
    created_time: datetime | None = None
    remaining_count: int = 0


class PortfolioSnapshot(BaseModel):
    """Point-in-time snapshot of the portfolio."""

    timestamp: datetime
    balance_cents: int
    positions: list[Position] = Field(default_factory=list)
    total_exposure_cents: int = 0
    unrealized_pnl_cents: int = 0
    daily_pnl_cents: int = 0


class PerformanceMetrics(BaseModel):
    """Aggregate performance metrics."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_cents: int = 0
    max_drawdown_cents: int = 0
    best_trade_pnl_cents: int = 0
    worst_trade_pnl_cents: int = 0

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades

    @property
    def average_pnl_cents(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl_cents / self.total_trades
