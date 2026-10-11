"""yfinance stock fallback adapter."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from urllib.parse import quote

import requests

from app.data.adapters.base import DataProvider, EmptyDataError, MarketData, ProviderError
from app.data.adapters.base import filter_market_data
from market_bot.store import cache_market_data, get_cached_market_data

LOGGER = logging.getLogger(__name__)
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

_YFINANCE_INTERVALS = {
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "60min": "60m",
    "daily": "1d",
}


class YFinanceProvider(DataProvider):
    def __init__(self, database_path: Path, cache_minutes: int = 15) -> None:
        self.database_path = database_path
        self.cache_minutes = cache_minutes
        self._metadata_cache: dict[str, dict[str, Any]] = {}

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "daily",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketData]:
        timeframe = timeframe.lower()
        if timeframe not in _YFINANCE_INTERVALS:
            raise ValueError(f"yfinance does not support timeframe {timeframe}.")
        symbol = symbol.strip().upper()
        cached = get_cached_market_data(
            self.database_path,
            symbol,
            timeframe,
            datetime.now(timezone.utc) - timedelta(minutes=self.cache_minutes),
        )
        if cached:
            return filter_market_data(cached, start, end)
        interval = _YFINANCE_INTERVALS[timeframe]
        params: dict[str, str | int] = {
            "interval": interval,
            "range": "max" if timeframe == "daily" else "60d",
            "events": "history",
        }
        if start is not None:
            if start.tzinfo is None or start.utcoffset() is None:
                raise ValueError("start must include a timezone")
            params["period1"] = int(start.timestamp())
        if end is not None:
            if end.tzinfo is None or end.utcoffset() is None:
                raise ValueError("end must include a timezone")
            params["period2"] = int(end.timestamp())
        if "period1" in params or "period2" in params:
            params.pop("range", None)
        started = perf_counter()
        try:
            response = requests.get(
                f"{_YAHOO_CHART_URL}/{quote(symbol, safe='')}",
                params=params,
                headers={"User-Agent": "Mozilla/5.0 MarketIntelligenceBot/1.0"},
                timeout=15,
            )
        except requests.Timeout:
            raise ProviderError("Yahoo Finance stock-history request timed out.") from None
        except requests.RequestException:
            raise ProviderError("Yahoo Finance stock-history request failed.") from None
        latency_ms = (perf_counter() - started) * 1000
        if response.status_code >= 500:
            raise ProviderError(f"Yahoo Finance returned HTTP {response.status_code}.")
        if response.status_code >= 400:
            raise ProviderError(f"Yahoo Finance rejected the symbol {symbol}.")
        try:
            payload: object = response.json()
        except (ValueError, requests.JSONDecodeError):
            raise ProviderError("Yahoo Finance returned malformed JSON.") from None
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in cast(dict[object, object], payload).keys()
        ):
            raise ProviderError("Yahoo Finance returned an invalid response.")
        response_data = cast(dict[str, Any], payload)
        chart_value: object = response_data.get("chart")
        chart = cast(dict[str, Any], chart_value) if isinstance(chart_value, dict) else {}
        result_value: object = chart.get("result")
        result_list = cast(list[Any], result_value) if isinstance(result_value, list) else []
        if not result_list:
            error_value: object = chart.get("error")
            error = cast(dict[str, Any], error_value) if isinstance(error_value, dict) else {}
            description = error.get("description")
            if description:
                raise ProviderError(f"Yahoo Finance returned an error for {symbol}: {description}")
            raise EmptyDataError(f"Yahoo Finance returned no candles for {symbol}.")
        result_value = result_list[0]
        if not isinstance(result_value, dict) or not all(
            isinstance(key, str) for key in cast(dict[object, object], result_value).keys()
        ):
            raise ProviderError("Yahoo Finance returned an invalid chart result.")
        result = cast(dict[str, Any], result_value)
        timestamps_value: object = result.get("timestamp")
        indicators = result.get("indicators")
        indicator_data = cast(dict[str, Any], indicators) if isinstance(indicators, dict) else {}
        quotes_value: object = indicator_data.get("quote")
        quotes = cast(list[Any], quotes_value) if isinstance(quotes_value, list) else []
        quote_data = quotes[0] if quotes else None
        if (
            not isinstance(timestamps_value, list)
            or not isinstance(quote_data, dict)
            or not all(isinstance(key, str) for key in cast(dict[object, object], quote_data).keys())
        ):
            raise ProviderError("Yahoo Finance response is missing candle fields.")
        timestamp_values = cast(list[object], timestamps_value)
        if not all(isinstance(value, (int, float)) for value in timestamp_values):
            raise ProviderError("Yahoo Finance response has invalid timestamps.")
        timestamps = cast(list[int | float], timestamp_values)
        quote_data = cast(dict[str, Any], quote_data)
        metadata_value: object = result.get("meta")
        metadata = cast(dict[str, Any], metadata_value) if isinstance(metadata_value, dict) else {}
        exchange = str(metadata.get("exchangeName", ""))
        columns: dict[str, list[Any]] = {}
        for field in ("open", "high", "low", "close", "volume"):
            value = quote_data.get(field)
            if not isinstance(value, list):
                raise ProviderError(f"Yahoo Finance response is missing {field} values.")
            columns[field] = cast(list[Any], value)
        records: list[MarketData] = []
        for index, epoch in enumerate(timestamps):
            try:
                item = MarketData(
                    timestamp=datetime.fromtimestamp(float(epoch), tz=timezone.utc),
                    symbol=symbol,
                    exchange=exchange,
                    timeframe=timeframe,
                    open=float(columns["open"][index]),
                    high=float(columns["high"][index]),
                    low=float(columns["low"][index]),
                    close=float(columns["close"][index]),
                    volume=float(columns["volume"][index] or 0),
                    source="yfinance",
                )
            except (IndexError, TypeError, ValueError, OSError):
                continue
            records.append(item)
        if not records:
            raise EmptyDataError(f"Yahoo Finance returned no valid candles for {symbol}.")
        records_by_timestamp: dict[datetime, MarketData] = {
            record.timestamp: record for record in records
        }
        records = sorted(records_by_timestamp.values(), key=lambda item: item.timestamp)
        cache_market_data(self.database_path, records)
        LOGGER.info(
            "provider=yfinance symbol=%s timeframe=%s request_status=success records=%d latency_ms=%.1f",
            symbol,
            timeframe,
            len(records),
            latency_ms,
        )
        self._metadata_cache[symbol] = {"symbol": symbol, "exchange": exchange}
        return filter_market_data(records, start, end)

    def get_latest_data(self, symbols: list[str], timeframe: str = "daily") -> dict[str, MarketData]:
        return {
            symbol.strip().upper(): bars[-1]
            for symbol in symbols
            if (bars := self.get_historical_data(symbol, timeframe))
        }

    def get_asset_metadata(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        if symbol not in self._metadata_cache:
            self.get_historical_data(symbol, "daily")
        return {"source": "yfinance", **self._metadata_cache.get(symbol, {"symbol": symbol})}
