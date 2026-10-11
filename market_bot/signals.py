"""Configurable simple-moving-average crossover strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.data.adapters.base import MarketData


@dataclass(frozen=True)
class Signal:
    symbol: str
    timestamp: datetime
    direction: str
    close: float
    fast_average: float
    slow_average: float


def _average(values: list[float]) -> float:
    return sum(values) / len(values)


def moving_average_crossover(
    bars: list[MarketData],
    fast_window: int,
    slow_window: int,
    max_data_age_minutes: int,
    now: datetime | None = None,
) -> Signal | None:
    """Return a fresh BUY/SELL crossover, or None until one is validated."""
    if fast_window < 1 or slow_window <= fast_window:
        raise ValueError("Windows must satisfy 1 <= fast_window < slow_window")
    if max_data_age_minutes < 1:
        raise ValueError("max_data_age_minutes must be positive")
    if len(bars) < slow_window + 1:
        return None

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must include a timezone")
    latest = bars[-1]
    current_time = current_time.astimezone(timezone.utc)
    if latest.timestamp < current_time - timedelta(minutes=max_data_age_minutes):
        return None
    if latest.timestamp > current_time + timedelta(minutes=5):
        return None

    closes = [bar.close for bar in bars]
    previous_fast = _average(closes[-fast_window - 1:-1])
    previous_slow = _average(closes[-slow_window - 1:-1])
    current_fast = _average(closes[-fast_window:])
    current_slow = _average(closes[-slow_window:])

    direction: str | None = None
    if previous_fast <= previous_slow and current_fast > current_slow:
        direction = "BUY"
    elif previous_fast >= previous_slow and current_fast < current_slow:
        direction = "SELL"
    if direction is None:
        return None

    return Signal(
        symbol=latest.symbol,
        timestamp=latest.timestamp,
        direction=direction,
        close=latest.close,
        fast_average=current_fast,
        slow_average=current_slow,
    )
