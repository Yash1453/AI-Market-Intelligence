"""Independent market scan, signal, and paper-performance loops."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.data.adapters.base import MarketData, MissingAPIKeyError
from app.data.provider import create_provider, load_histories
from market_bot.config import Settings
from market_bot.signals import moving_average_crossover
from market_bot.store import (
    pending_predictions,
    pending_signal_alerts,
    latest_prediction_timestamps,
    prediction_metrics,
    mark_signal_sent,
    record_prediction,
    record_signal,
    settle_prediction,
)
from market_bot.telegram import send_message

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Performance:
    evaluated: int
    hits: int
    hit_rate: float | None
    average_return: float | None
    pending: int


def _format_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _load_market_histories(settings: Settings) -> dict[str, list[MarketData]]:
    provider = create_provider(settings)
    symbols = settings.scan_symbols[: settings.max_symbols_per_scan]
    return load_histories(provider, symbols, settings.timeframe)


def run_market_report(settings: Settings, now: datetime | None = None) -> Path:
    histories = _load_market_histories(settings)
    report_time = now or datetime.now(timezone.utc)
    if report_time.tzinfo is None or report_time.utcoffset() is None:
        raise ValueError("now must include a timezone")
    report_time = report_time.astimezone(timezone.utc)
    rows = [
        f"# Market snapshot — {report_time.isoformat()}",
        "",
        "Paper-analysis only; this report is not investment advice.",
        "",
        "| Symbol | Latest close | Timestamp (UTC) | 24h change | Feed status |",
        "|---|---:|---|---:|---|",
    ]
    for symbol, bars in sorted(histories.items()):
        latest = bars[-1]
        cutoff = latest.timestamp - timedelta(hours=24)
        previous = next((bar for bar in reversed(bars) if bar.timestamp <= cutoff), None)
        change = (
            (latest.close - previous.close) / previous.close
            if previous is not None and previous.timestamp < latest.timestamp
            else None
        )
        age = report_time - latest.timestamp
        if age < timedelta(minutes=-5):
            status = "future timestamp"
        elif age <= timedelta(minutes=settings.max_data_age_minutes):
            status = "fresh"
        else:
            status = "stale"
        rows.append(
            f"| {symbol} | {latest.close:.8g} | {latest.timestamp.isoformat()} | "
            f"{_format_pct(change)} | {status} |"
        )
    if not histories:
        rows.append("| — | — | — | — | no data |")

    settings.report_dir.mkdir(parents=True, exist_ok=True)
    file_timestamp = report_time.strftime("%Y%m%dT%H%M%SZ")
    path = settings.report_dir / f"market_report_{file_timestamp}.md"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    LOGGER.info("Wrote hourly market report to %s", path)
    return path


def run_signal_scan(settings: Settings, now: datetime | None = None) -> int:
    histories = _load_market_histories(settings)
    new_signals = 0
    for symbol, bars in sorted(histories.items()):
        signal = moving_average_crossover(
            bars,
            fast_window=settings.fast_window,
            slow_window=settings.slow_window,
            max_data_age_minutes=settings.max_data_age_minutes,
            now=now,
        )
        if signal is not None:
            new_signals += int(record_signal(settings.database_path, signal))

    alerts = pending_signal_alerts(settings.database_path)
    if not alerts:
        return new_signals
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        for signal in alerts:
            LOGGER.warning(
                "Telegram is not configured; validated signal stored but not sent: %s %s",
                signal.direction,
                signal.symbol,
            )
        return new_signals

    for signal in alerts:
        message = (
            f"Validated paper signal: {signal.direction} {signal.symbol}\n"
            f"Close: {signal.close:.8g}\n"
            f"Fast SMA ({settings.fast_window}): {signal.fast_average:.8g}\n"
            f"Slow SMA ({settings.slow_window}): {signal.slow_average:.8g}\n"
            f"Timestamp: {signal.timestamp.isoformat()}\n"
            "Paper analysis only; no order was placed."
        )
        send_message(settings.telegram_bot_token, settings.telegram_chat_id, message)
        mark_signal_sent(settings.database_path, signal)
        LOGGER.info("Telegram alert sent for %s %s", signal.direction, signal.symbol)
    return new_signals


def run_performance_cycle(settings: Settings) -> Performance:
    histories = _load_market_histories(settings)
    now = datetime.now(timezone.utc)
    latest_predictions = latest_prediction_timestamps(settings.database_path)
    for symbol, bars in histories.items():
        previous_prediction = latest_predictions.get(symbol)
        if previous_prediction is None:
            start_index = len(bars) - 1
        else:
            start_index = next(
                (
                    index + 1
                    for index, bar in enumerate(bars)
                    if bar.timestamp == previous_prediction
                ),
                len(bars) - 1,
            )
        for index in range(start_index, len(bars)):
            bar = bars[index]
            if bar.timestamp < now - timedelta(minutes=settings.max_data_age_minutes):
                continue
            if bar.timestamp > now + timedelta(minutes=5):
                continue
            if index + 1 < settings.slow_window:
                continue
            observed = bars[: index + 1]
            fast_average = sum(
                item.close for item in observed[-settings.fast_window:]
            ) / settings.fast_window
            slow_average = sum(
                item.close for item in observed[-settings.slow_window:]
            ) / settings.slow_window
            direction = 1 if fast_average >= slow_average else -1
            record_prediction(settings.database_path, bar, direction)

    for prediction in pending_predictions(settings.database_path):
        bars = histories.get(prediction.symbol, [])
        index = next(
            (i for i, bar in enumerate(bars) if bar.timestamp == prediction.timestamp),
            None,
        )
        if index is None or index + settings.hold_bars >= len(bars):
            continue
        settle_prediction(settings.database_path, prediction, bars[index + settings.hold_bars].close)

    evaluated, hits, hit_rate, average_return = prediction_metrics(settings.database_path)
    pending_count = len(pending_predictions(settings.database_path))
    performance = Performance(evaluated, hits, hit_rate, average_return, pending_count)
    LOGGER.info(
        "Paper performance: evaluated=%d hits=%d hit_rate=%s average_return=%s pending=%d",
        evaluated,
        hits,
        _format_pct(hit_rate),
        _format_pct(average_return),
        pending_count,
    )
    if evaluated >= settings.monitor_min_predictions:
        if hit_rate is not None and hit_rate < settings.monitor_min_hit_rate:
            LOGGER.warning(
                "Model monitoring: paper hit rate %.2f%% is below the %.2f%% threshold",
                hit_rate * 100,
                settings.monitor_min_hit_rate * 100,
            )
    else:
        LOGGER.info(
            "Model monitoring warming up: %d/%d evaluated predictions",
            evaluated,
            settings.monitor_min_predictions,
        )
    return performance


def run_selected_loop(settings: Settings, loop: str) -> None:
    if loop == "report":
        run_market_report(settings)
    elif loop == "signals":
        run_signal_scan(settings)
    elif loop == "performance":
        run_performance_cycle(settings)
    elif loop == "all":
        run_market_report(settings)
        run_signal_scan(settings)
        run_performance_cycle(settings)
    else:
        raise ValueError(f"Unknown market bot loop: {loop}")


def run_loop(settings: Settings, loop: str, once: bool = False) -> None:
    intervals = {
        "report": settings.report_interval_seconds,
        "signals": settings.signal_interval_seconds,
        "performance": settings.performance_interval_seconds,
    }
    if loop != "all" and loop not in intervals:
        raise ValueError(f"Unknown market bot loop: {loop}")
    if once:
        run_selected_loop(settings, loop)
        return

    selected = tuple(intervals) if loop == "all" else (loop,)
    next_runs = {name: 0.0 for name in selected}
    while True:
        now = time.monotonic()
        due = [name for name in selected if now >= next_runs[name]]
        for name in due:
            try:
                run_selected_loop(settings, name)
            except MissingAPIKeyError:
                raise
            except Exception:
                LOGGER.exception(
                    "Market bot %s loop failed; it will retry after %d seconds",
                    name,
                    intervals[name],
                )
            next_runs[name] = time.monotonic() + intervals[name]
        wait = max(0.0, min(next_runs.values()) - time.monotonic())
        time.sleep(wait)
