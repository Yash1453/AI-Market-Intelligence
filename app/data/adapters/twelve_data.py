"""Twelve Data adapter for stock, forex, and cryptocurrency candles."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import requests

from app.data.adapters.base import (
    DataProvider,
    EmptyDataError,
    InvalidAPIKeyError,
    MarketData,
    MalformedResponseError,
    NetworkError,
    ProviderError,
    ProviderOutageError,
    RateLimitError,
    filter_market_data,
)
from app.data.adapters.secrets import read_market_secret, validate_https_base_url
from market_bot.store import cache_market_data, get_cached_market_data

LOGGER = logging.getLogger(__name__)
_INTERVALS = {
    "1min": "1min",
    "5min": "5min",
    "15min": "15min",
    "30min": "30min",
    "60min": "1h",
    "daily": "1day",
}


class TwelveDataProvider(DataProvider):
    def __init__(
        self,
        database_path: Path,
        base_url: str = "https://api.twelvedata.com",
        cache_minutes: int = 15,
        request_delay: float = 1.0,
        timeout: float = 15.0,
    ) -> None:
        self.database_path = database_path
        self.base_url = validate_https_base_url(base_url, "TWELVE_DATA_BASE_URL")
        self.cache_minutes = cache_minutes
        self.request_delay = request_delay
        self.timeout = timeout
        self._last_request_at: float | None = None
        self._metadata: dict[str, dict[str, Any]] = {}

    def _api_key(self) -> str:
        return read_market_secret(
            "TWELVE_DATA_API_KEY",
            "Twelve Data API key is missing. Add TWELVE_DATA_API_KEY to market_bot/.env.",
        )

    def _get_json(self, params: dict[str, str], symbol: str) -> dict[str, Any]:
        if self._last_request_at is not None:
            wait = self.request_delay - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        started = time.perf_counter()
        try:
            response = requests.get(
                f"{self.base_url}/time_series",
                params={**params, "apikey": self._api_key()},
                timeout=self.timeout,
            )
            self._last_request_at = time.monotonic()
        except requests.Timeout:
            raise NetworkError("Twelve Data request timed out.") from None
        except requests.RequestException:
            raise NetworkError("Twelve Data network request failed.") from None
        latency_ms = (time.perf_counter() - started) * 1000
        if response.status_code == 429:
            raise RateLimitError("Twelve Data rate limit reached.")
        if response.status_code >= 500:
            raise ProviderOutageError(f"Twelve Data returned HTTP {response.status_code}.")
        try:
            payload: object = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise MalformedResponseError("Twelve Data returned malformed JSON.") from exc
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in cast(dict[object, object], payload).keys()
        ):
            raise MalformedResponseError("Twelve Data response must be a JSON object.")
        result = cast(dict[str, Any], payload)
        if result.get("status") == "error" or result.get("code") not in (None, 200):
            message = str(result.get("message", "Twelve Data returned an API error."))
            api_key = self._api_key()
            message = message.replace(api_key, "[REDACTED]")
            message = re.sub(r"(?i)(apikey\s*[=:]\s*)[^&\s,]+", r"\1[REDACTED]", message)
            message = re.sub(r"https?://\S+", "[URL REDACTED]", message)
            lowered = message.lower()
            if "api key" in lowered or "apikey" in lowered or "unauthorized" in lowered:
                raise InvalidAPIKeyError("Twelve Data rejected the configured API key.")
            if "limit" in lowered or "credits" in lowered:
                raise RateLimitError("Twelve Data rate limit reached.")
            raise ProviderError(f"Twelve Data error: {message}")
        LOGGER.info(
            "provider=twelve_data symbol=%s request_status=success records=%d latency_ms=%.1f",
            symbol,
            len(result.get("values", [])) if isinstance(result.get("values"), list) else 0,
            latency_ms,
        )
        return result

    @staticmethod
    def _timestamp(value: str, market_timezone: str = "UTC") -> datetime:
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                if market_timezone.strip().lower() in {"utc", "etc/utc", "gmt"}:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                else:
                    from zoneinfo import ZoneInfo

                    timestamp = timestamp.replace(tzinfo=ZoneInfo(market_timezone))
        except (ValueError, KeyError) as exc:
            raise MalformedResponseError("Twelve Data returned an invalid timestamp or timezone.") from exc
        return timestamp.astimezone(timezone.utc)

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "daily",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketData]:
        timeframe = timeframe.lower()
        if timeframe not in _INTERVALS:
            raise ValueError(f"Twelve Data does not support timeframe {timeframe}.")
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol cannot be empty")
        cached = get_cached_market_data(
            self.database_path,
            symbol,
            timeframe,
            datetime.now(timezone.utc) - timedelta(minutes=self.cache_minutes),
        )
        if cached:
            return filter_market_data(cached, start, end)
        params = {
            "symbol": symbol,
            "interval": _INTERVALS[timeframe],
            "outputsize": "5000",
            "format": "JSON",
            "timezone": "UTC",
        }
        payload = self._get_json(params, symbol)
        values_value: object = payload.get("values")
        if not isinstance(values_value, list):
            raise MalformedResponseError("Twelve Data response is missing its values array.")
        raw_values = cast(list[object], values_value)
        if not all(isinstance(row, dict) for row in raw_values):
            raise MalformedResponseError("Twelve Data response is missing its values array.")
        values = cast(list[dict[str, Any]], values_value)
        if not values:
            raise EmptyDataError(f"Twelve Data returned no candles for {symbol}.")
        metadata_value: object = payload.get("meta")
        metadata = (
            cast(dict[str, Any], metadata_value)
            if isinstance(metadata_value, dict)
            else {}
        )
        exchange = str(metadata.get("exchange", ""))
        timezone_name = str(metadata.get("exchange_timezone", "UTC"))
        records_by_time: dict[datetime, MarketData] = {}
        for row in values:
            try:
                candle = MarketData(
                    timestamp=self._timestamp(str(row["datetime"]), timezone_name),
                    symbol=symbol,
                    exchange=exchange,
                    timeframe=timeframe,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                    source="twelve_data",
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MalformedResponseError("Twelve Data returned invalid OHLCV data.") from exc
            prior = records_by_time.get(candle.timestamp)
            if prior is not None and prior != candle:
                raise MalformedResponseError("Twelve Data returned conflicting duplicate candles.")
            records_by_time[candle.timestamp] = candle
        records = sorted(records_by_time.values(), key=lambda item: item.timestamp)
        cache_market_data(self.database_path, records)
        self._metadata[symbol] = dict(metadata)
        records = filter_market_data(records, start, end)
        if not records:
            raise EmptyDataError(f"Twelve Data returned no candles in the requested range for {symbol}.")
        return records

    def get_latest_data(self, symbols: list[str], timeframe: str = "daily") -> dict[str, MarketData]:
        return {
            symbol.strip().upper(): bars[-1]
            for symbol in symbols
            if (bars := self.get_historical_data(symbol, timeframe))
        }

    def get_asset_metadata(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        if symbol not in self._metadata:
            self.get_historical_data(symbol, "daily")
        return {"symbol": symbol, "source": "twelve_data", **self._metadata.get(symbol, {})}
