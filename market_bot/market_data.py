"""Compatibility helpers for the shared market-data model."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.data.adapters.base import MarketData
from app.data.adapters.csv_provider import CSVProvider
from app.data.provider import load_histories


class PriceBar(MarketData):
    """Legacy close-only constructor retained for the existing engine tests."""

    def __init__(
        self,
        timestamp: datetime,
        symbol: str,
        close: float,
        exchange: str = "",
        timeframe: str = "daily",
        open: float | None = None,
        high: float | None = None,
        low: float | None = None,
        volume: float = 0.0,
        source: str = "csv",
    ) -> None:
        super().__init__(
            timestamp=timestamp,
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
            open=close if open is None else open,
            high=close if high is None else high,
            low=close if low is None else low,
            close=close,
            volume=volume,
            source=source,
        )


def load_bars(path: Path) -> dict[str, list[MarketData]]:
    """Load every symbol from a CSV into the normalized market-data schema."""
    provider = CSVProvider(path)
    return load_histories(provider, None, provider.default_timeframe)
