"""Circuit breaker — emergency stop conditions that halt all trading."""

from __future__ import annotations

import time
from collections import deque

import structlog

from kalshi_bot.config import RiskConfig

logger = structlog.get_logger(__name__)


class CircuitBreaker:
    """Monitors for emergency conditions and trips to halt trading.

    Trigger conditions:
    - Consecutive losing trades exceed threshold
    - Hourly loss rate exceeds threshold
    - API error rate exceeds threshold
    """

    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self._tripped = False
        self._trip_reason = ""
        self._consecutive_losses = 0
        self._hourly_losses: deque[tuple[float, int]] = deque()  # (timestamp, loss_cents)
        self._api_calls: deque[tuple[float, bool]] = deque()  # (timestamp, was_error)

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    def trip(self, reason: str) -> None:
        """Manually trip the circuit breaker."""
        self._tripped = True
        self._trip_reason = reason
        logger.critical("circuit_breaker_tripped", reason=reason)

    def reset(self) -> None:
        """Reset the circuit breaker (requires manual intervention)."""
        self._tripped = False
        self._trip_reason = ""
        self._consecutive_losses = 0
        self._hourly_losses.clear()
        self._api_calls.clear()
        logger.info("circuit_breaker_reset")

    def record_trade_result(self, pnl_cents: int) -> None:
        """Record a trade result. Negative pnl = loss."""
        if pnl_cents < 0:
            self._consecutive_losses += 1
            self._hourly_losses.append((time.monotonic(), abs(pnl_cents)))

            # Check consecutive losses
            if self._consecutive_losses >= self.config.consecutive_loss_limit:
                self.trip(
                    f"Consecutive losses: {self._consecutive_losses} >= {self.config.consecutive_loss_limit}"
                )
                return

            # Check hourly loss rate
            self._prune_hourly_losses()
            hourly_total = sum(loss for _, loss in self._hourly_losses)
            if hourly_total >= self.config.hourly_loss_limit_cents:
                self.trip(
                    f"Hourly loss: {hourly_total}c >= {self.config.hourly_loss_limit_cents}c"
                )
                return
        else:
            self._consecutive_losses = 0

    def record_api_call(self, was_error: bool) -> None:
        """Record an API call result for error rate monitoring."""
        self._api_calls.append((time.monotonic(), was_error))
        self._prune_api_calls()

        if len(self._api_calls) >= 10:  # Need minimum sample
            error_count = sum(1 for _, err in self._api_calls if err)
            error_rate = error_count / len(self._api_calls)
            if error_rate >= self.config.api_error_rate_limit:
                self.trip(f"API error rate: {error_rate:.0%} >= {self.config.api_error_rate_limit:.0%}")

    def _prune_hourly_losses(self) -> None:
        """Remove loss records older than 1 hour."""
        cutoff = time.monotonic() - 3600
        while self._hourly_losses and self._hourly_losses[0][0] < cutoff:
            self._hourly_losses.popleft()

    def _prune_api_calls(self) -> None:
        """Remove API call records older than 5 minutes."""
        cutoff = time.monotonic() - 300
        while self._api_calls and self._api_calls[0][0] < cutoff:
            self._api_calls.popleft()
