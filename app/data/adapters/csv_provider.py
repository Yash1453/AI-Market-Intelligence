"""CSV provider that returns the shared normalized market-data model."""

from __future__ import annotations

import csv
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.data.adapters.base import DataProvider, MarketData, MalformedResponseError

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._:/-]*$")


def _parse_timestamp(value: str, line_number: int) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp on CSV line {line_number}") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"Timestamp must include a timezone on CSV line {line_number}")
    return timestamp.astimezone(timezone.utc)


def _number(row: dict[str, str | None], key: str, line_number: int, default: float | None = None) -> float:
    raw = row.get(key)
    if raw is None or not raw.strip():
        if default is not None:
            return default
        raise MalformedResponseError(f"Missing {key} on CSV line {line_number}")
    try:
        number = float(raw)
    except ValueError as exc:
        raise MalformedResponseError(f"Invalid {key} on CSV line {line_number}") from exc
    if not math.isfinite(number):
        raise MalformedResponseError(f"Invalid {key} on CSV line {line_number}")
    return number


class CSVProvider(DataProvider):
    def __init__(self, path: Path, default_timeframe: str = "daily") -> None:
        self.path = path
        self.default_timeframe = default_timeframe

    def get_available_symbols(self) -> list[str]:
        if not self.path.is_file():
            raise FileNotFoundError(f"Market CSV not found: {self.path}")
        with self.path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames is None or not {"timestamp", "symbol", "close"}.issubset(reader.fieldnames):
                raise MalformedResponseError(
                    "Market CSV must include timestamp, symbol, and close columns"
                )
            symbols = {
                (row.get("symbol") or "").strip().upper()
                for row in reader
            }
        if any(not _SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
            raise MalformedResponseError("Market CSV contains an invalid or missing symbol")
        return sorted(symbols)

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "daily",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketData]:
        if not self.path.is_file():
            raise FileNotFoundError(f"Market CSV not found: {self.path}")
        requested_symbol = symbol.strip().upper()
        data: dict[tuple[str, datetime, str], MarketData] = {}
        with self.path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            required = {"timestamp", "symbol", "close"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise MalformedResponseError(
                    "Market CSV must include timestamp, symbol, and close columns"
                )
            for row in reader:
                line_number = reader.line_num
                row_symbol = (row.get("symbol") or "").strip().upper()
                if not _SYMBOL_PATTERN.fullmatch(row_symbol):
                    raise MalformedResponseError(f"Invalid or missing symbol on CSV line {line_number}")
                row_timeframe = (row.get("timeframe") or self.default_timeframe).strip().lower()
                if row_symbol != requested_symbol or row_timeframe != timeframe.lower():
                    continue
                timestamp = _parse_timestamp(row.get("timestamp") or "", line_number)
                close = _number(row, "close", line_number)
                item = MarketData(
                    timestamp=timestamp,
                    symbol=row_symbol,
                    exchange=(row.get("exchange") or "").strip(),
                    timeframe=row_timeframe,
                    open=_number(row, "open", line_number, close),
                    high=_number(row, "high", line_number, close),
                    low=_number(row, "low", line_number, close),
                    close=close,
                    volume=_number(row, "volume", line_number, 0.0),
                    source="csv",
                )
                if start is not None and timestamp < start.astimezone(timezone.utc):
                    continue
                if end is not None and timestamp > end.astimezone(timezone.utc):
                    continue
                key = (row_symbol, timestamp, row_timeframe)
                if key in data:
                    if data[key] != item:
                        raise MalformedResponseError(
                            f"Conflicting duplicate candle on CSV line {line_number}"
                        )
                    continue
                data[key] = item
        return sorted(data.values(), key=lambda bar: bar.timestamp)

    def get_latest_data(
        self,
        symbols: list[str],
        timeframe: str = "daily",
    ) -> dict[str, MarketData]:
        latest: dict[str, MarketData] = {}
        for symbol in symbols:
            bars = self.get_historical_data(symbol, timeframe)
            if bars:
                latest[symbol.upper()] = bars[-1]
        return latest

    def get_asset_metadata(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        bars = self.get_historical_data(symbol, self.default_timeframe)
        if not bars:
            return {"symbol": symbol}
        return {
            "symbol": symbol,
            "exchange": bars[-1].exchange,
            "source": "csv",
            "records": len(bars),
        }
