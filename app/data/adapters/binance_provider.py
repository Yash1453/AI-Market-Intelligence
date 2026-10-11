"""Binance public REST adapter for cryptocurrency candles."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from time import perf_counter
from pathlib import Path
from typing import Any

import requests

from app.data.adapters.base import (
    DataProvider,
    EmptyDataError,
    MarketData,
    MalformedResponseError,
    NetworkError,
    ProviderOutageError,
    RateLimitError,
    filter_market_data,
)
from app.data.adapters.secrets import validate_https_base_url
from market_bot.store import cache_market_data, get_cached_market_data

_BINANCE_INTERVALS = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "60min": "1h",
    "daily": "1d",
}
LOGGER = logging.getLogger(__name__)


class BinanceProvider(DataProvider):
    def __init__(
        self,
        database_path: Path,
        base_url: str = "https://api.binance.com",
        cache_minutes: int = 15,
        timeout: float = 15.0,
    ) -> None:
        self.database_path = database_path
        self.base_url = validate_https_base_url(base_url, "BINANCE_BASE_URL")
        self.cache_minutes = cache_minutes
        self.timeout = timeout

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "daily",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketData]:
        timeframe = timeframe.lower()
        if timeframe not in _BINANCE_INTERVALS:
            raise ValueError(f"Binance does not support timeframe {timeframe}.")
        requested_symbol = symbol.strip().upper()
        symbol = requested_symbol.replace("/", "").replace("-", "")
        cached = get_cached_market_data(
            self.database_path,
            requested_symbol,
            timeframe,
            datetime.now(timezone.utc) - timedelta(minutes=self.cache_minutes),
        )
        if cached:
            return filter_market_data(cached, start, end)
        started = perf_counter()
        try:
            response = requests.get(
                f"{self.base_url}/api/v3/klines",
                params={"symbol": symbol, "interval": _BINANCE_INTERVALS[timeframe], "limit": 1000},
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise NetworkError("Binance market-data request timed out.") from exc
        except requests.RequestException as exc:
            raise NetworkError("Binance market-data request failed.") from exc
        latency_ms = (perf_counter() - started) * 1000
        if response.status_code == 400:
            raise MalformedResponseError(f"Binance rejected the configured symbol {requested_symbol}.")
        if response.status_code == 429:
            raise RateLimitError("Binance rate limit reached.")
        if response.status_code >= 500:
            raise ProviderOutageError(f"Binance returned HTTP {response.status_code}.")
        try:
            payload: object = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise MalformedResponseError("Binance returned malformed JSON.") from exc
        if not isinstance(payload, list):
            raise MalformedResponseError("Binance returned an invalid candle response.")
        if not payload:
            raise EmptyDataError(f"Binance returned no candles for {symbol}.")
        records: dict[datetime, MarketData] = {}
        for row in payload:
            if not isinstance(row, list) or len(row) < 6:
                raise MalformedResponseError("Binance candle has an invalid structure.")
            try:
                record = MarketData(
                    timestamp=datetime.fromtimestamp(float(row[0]) / 1000, tz=timezone.utc),
                    symbol=requested_symbol,
                    exchange="BINANCE",
                    timeframe=timeframe,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    source="binance",
                )
            except (TypeError, ValueError, OSError) as exc:
                raise MalformedResponseError("Binance returned invalid OHLCV data.") from exc
            if record.timestamp in records and records[record.timestamp] != record:
                raise MalformedResponseError("Binance returned conflicting duplicate candles.")
            records[record.timestamp] = record
        result = sorted(records.values(), key=lambda item: item.timestamp)
        cache_market_data(self.database_path, result)
        LOGGER.info(
            "provider=binance symbol=%s timeframe=%s request_status=success records=%d latency_ms=%.1f",
            requested_symbol,
            timeframe,
            len(result),
            latency_ms,
        )
        return filter_market_data(result, start, end)

    def get_latest_data(self, symbols: list[str], timeframe: str = "daily") -> dict[str, MarketData]:
        return {
            symbol: bars[-1]
            for symbol in symbols
            if (bars := self.get_historical_data(symbol, timeframe))
        }

    def get_asset_metadata(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol.upper(), "exchange": "BINANCE", "source": "binance"}
