"""Small, transparent quality metric for normalized market candles."""

from __future__ import annotations

from datetime import timedelta

from app.data.adapters.base import MarketData

_INTERVAL_MINUTES = {"5min": 5, "15min": 15, "30min": 30, "60min": 60}


def data_quality_percent(records: list[MarketData]) -> float:
    if not records:
        return 0.0
    ordered = sorted(records, key=lambda item: item.timestamp)
    interval = _INTERVAL_MINUTES.get(ordered[0].timeframe)
    missing = 0
    if interval is not None:
        expected = timedelta(minutes=interval)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.timestamp.date() != current.timestamp.date():
                continue
            gap = current.timestamp - previous.timestamp
            if gap > expected:
                missing += max(0, round(gap / expected) - 1)
    elif ordered[0].timeframe == "daily":
        for previous, current in zip(ordered, ordered[1:]):
            day = previous.timestamp.date()
            current_day = current.timestamp.date()
            while day < current_day:
                day += timedelta(days=1)
                if day.weekday() < 5 and day < current_day:
                    missing += 1
    return len(ordered) * 100.0 / (len(ordered) + missing)
