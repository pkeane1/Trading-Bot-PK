"""Custom exception types for the Kalshi API client."""

from __future__ import annotations


class KalshiAPIError(Exception):
    """Base exception for Kalshi API errors."""

    def __init__(self, status_code: int, message: str, response_body: str = "") -> None:
        self.status_code = status_code
        self.message = message
        self.response_body = response_body
        super().__init__(f"HTTP {status_code}: {message}")


class KalshiAuthError(KalshiAPIError):
    """Authentication or authorization failure (401/403)."""


class KalshiRateLimitError(KalshiAPIError):
    """Rate limit exceeded (429)."""

    def __init__(self, retry_after: float | None = None, **kwargs: object) -> None:
        self.retry_after = retry_after
        super().__init__(status_code=429, message="Rate limit exceeded")


class KalshiNotFoundError(KalshiAPIError):
    """Resource not found (404)."""


class KalshiValidationError(KalshiAPIError):
    """Request validation error (400)."""


class KalshiInsufficientFundsError(KalshiAPIError):
    """Insufficient balance for order (400 with specific code)."""


class KalshiServiceUnavailableError(KalshiAPIError):
    """Service temporarily unavailable (503) — retryable."""


class KalshiMarketClosedError(KalshiAPIError):
    """Market is closed — orders cannot be placed (409 market_closed)."""


def raise_for_status(status_code: int, body: str) -> None:
    """Raise the appropriate exception for an error status code."""
    if status_code < 400:
        return

    if status_code == 401 or status_code == 403:
        raise KalshiAuthError(status_code, "Authentication failed", body)
    elif status_code == 404:
        raise KalshiNotFoundError(status_code, "Not found", body)
    elif status_code == 429:
        raise KalshiRateLimitError()
    elif status_code == 503:
        raise KalshiServiceUnavailableError(status_code, "Service unavailable", body)
    elif status_code == 400:
        if "insufficient" in body.lower():
            raise KalshiInsufficientFundsError(status_code, "Insufficient funds", body)
        raise KalshiValidationError(status_code, "Validation error", body)
    elif status_code == 409:
        if "market_closed" in body.lower():
            raise KalshiMarketClosedError(status_code, "Market closed", body)
        raise KalshiAPIError(status_code, "Conflict", body)
    else:
        raise KalshiAPIError(status_code, "API error", body)
