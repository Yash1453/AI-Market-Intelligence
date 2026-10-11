"""Common market-data model and provider contract."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class ProviderError(RuntimeError):
    """Base class for expected market-data provider failures."""


class ProviderDiagnosticError(ProviderError):
    """Provider response error with credential-safe request and response diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        request_params: dict[str, str],
        response_keys: list[str],
        response_messages: dict[str, str],
        response_type: str,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.request_params = dict(request_params)
        self.response_keys = tuple(response_keys)
        self.response_messages = dict(response_messages)
        self.response_type = response_type


class MissingAPIKeyError(ProviderError):
    """Raised when the Alpha Vantage key is absent from the bot's local .env."""


class InvalidAPIKeyError(ProviderError):
    """Raised when a provider rejects the configured API key."""


class RateLimitError(ProviderError):
    """Raised when the provider's request allowance is exhausted."""


class EmptyDataError(ProviderError):
    """Raised when the provider returned no usable candles."""


class MalformedResponseError(ProviderError):
    """Raised when provider data cannot be safely normalized."""


class NetworkError(ProviderError):
    """Raised when a network request fails after bounded retries."""


class ProviderOutageError(ProviderError):
    """Raised when the provider is unavailable after bounded retries."""


@dataclass(frozen=True)
class MarketData:
    timestamp: datetime
    symbol: str
    exchange: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Market data timestamp must include a timezone")
        if not self.symbol.strip():
            raise ValueError("Market data symbol cannot be empty")
        if not self.timeframe.strip() or not self.source.strip():
            raise ValueError("Market data timeframe and source cannot be empty")
        ohlc = (self.open, self.high, self.low, self.close)
        if not all(math.isfinite(value) and value > 0 for value in ohlc):
            raise ValueError("OHLC values must be finite and positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if self.low > self.high:
            raise ValueError("Low price cannot exceed high price")
        if not math.isfinite(self.volume) or self.volume < 0:
            raise ValueError("Volume must be finite and non-negative")


class DataProvider(ABC):
    """Provider interface; adapters return the same normalized candle model."""

    @abstractmethod
    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "daily",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketData]:
        """Get candles for one asset and timeframe."""

    @abstractmethod
    def get_latest_data(
        self,
        symbols: list[str],
        timeframe: str = "daily",
    ) -> dict[str, MarketData]:
        """Get the latest candle for each requested asset."""

    @abstractmethod
    def get_asset_metadata(self, symbol: str) -> dict[str, Any]:
        """Get provider metadata for one asset."""


def filter_market_data(
    records: list[MarketData],
    start: datetime | None,
    end: datetime | None,
) -> list[MarketData]:
    if start is not None and (start.tzinfo is None or start.utcoffset() is None):
        raise ValueError("start must include a timezone")
    if end is not None and (end.tzinfo is None or end.utcoffset() is None):
        raise ValueError("end must include a timezone")
    start_utc = start.astimezone(timezone.utc) if start else None
    end_utc = end.astimezone(timezone.utc) if end else None
    return [
        record
        for record in records
        if (start_utc is None or record.timestamp >= start_utc)
        and (end_utc is None or record.timestamp <= end_utc)
    ]
