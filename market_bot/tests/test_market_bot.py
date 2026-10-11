"""Offline tests for the independent market-intelligence bot."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from market_bot.config import Settings
from market_bot.engine import (
    run_market_report,
    run_performance_cycle,
    run_signal_scan,
)
from market_bot.market_data import PriceBar, load_bars
from market_bot.signals import Signal, moving_average_crossover
from market_bot.store import pending_signal_alerts, prediction_metrics, record_prediction, record_signal


def settings_for(temporary: str, telegram: bool = False) -> Settings:
    return Settings(
        csv_path=Path(temporary) / "prices.csv",
        database_path=Path(temporary) / "market.sqlite3",
        report_dir=Path(temporary) / "reports",
        fast_window=2,
        slow_window=3,
        hold_bars=2,
        max_data_age_minutes=60,
        signal_interval_seconds=60,
        performance_interval_seconds=300,
        report_interval_seconds=3600,
        monitor_min_predictions=3,
        monitor_min_hit_rate=0.45,
        telegram_bot_token="token" if telegram else None,
        telegram_chat_id="chat" if telegram else None,
        asset_symbols=("TEST",),
        scan_symbols=("TEST",),
    )


class MarketBotTests(unittest.TestCase):
    def test_csv_is_normalized_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prices.csv"
            path.write_text(
                "timestamp,symbol,close\n"
                "2026-10-04T09:01:00+00:00,example,102\n"
                "2026-10-04T09:00:00Z,EXAMPLE,101\n",
                encoding="utf-8",
            )
            bars = load_bars(path)["EXAMPLE"]
            self.assertEqual([bar.close for bar in bars], [101, 102])

    def test_csv_rejects_timezone_free_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "prices.csv"
            path.write_text(
                "timestamp,symbol,close\n2026-10-04T09:00:00,EXAMPLE,101\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "timezone"):
                load_bars(path)

    def test_crossover_requires_fresh_prices_and_detects_buy(self) -> None:
        now = datetime(2026, 10, 4, 9, 5, tzinfo=timezone.utc)
        closes = [5.0, 4.0, 3.0, 2.0, 1.0, 4.0]
        bars = [
            PriceBar(now - timedelta(minutes=len(closes) - i - 1), "TEST", close)
            for i, close in enumerate(closes)
        ]
        signal = moving_average_crossover(bars, 2, 3, 10, now=now)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "BUY")
        self.assertIsNone(
            moving_average_crossover(bars, 2, 3, 1, now=now + timedelta(minutes=2))
        )

    def test_signal_storage_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "market.sqlite3"
            signal = Signal(
                "TEST",
                datetime(2026, 10, 4, tzinfo=timezone.utc),
                "BUY",
                100.0,
                99.0,
                98.0,
            )
            self.assertTrue(record_signal(path, signal))
            self.assertFalse(record_signal(path, signal))

    def test_telegram_failure_leaves_signal_queued_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(temporary, telegram=True)
            timestamp = datetime.now(timezone.utc)
            closes = (5.0, 4.0, 3.0, 2.0, 1.0, 4.0)
            settings.csv_path.write_text(
                "timestamp,symbol,close\n"
                + "".join(
                    f"{(timestamp - timedelta(minutes=len(closes) - index - 1)).isoformat()},TEST,{close}\n"
                    for index, close in enumerate(closes)
                ),
                encoding="utf-8",
            )
            with patch(
                "market_bot.engine.send_message",
                side_effect=RuntimeError("temporary network failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "temporary network failure"):
                    run_signal_scan(settings, now=timestamp)
            self.assertEqual(len(pending_signal_alerts(settings.database_path)), 1)

            with patch("market_bot.engine.send_message") as send_message:
                run_signal_scan(settings, now=timestamp)
            send_message.assert_called_once()
            self.assertEqual(pending_signal_alerts(settings.database_path), [])

    def test_performance_settles_predictions_after_hold_bars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(temporary)
            timestamp = datetime.now(timezone.utc)
            settings.csv_path.write_text(
                "timestamp,symbol,close\n"
                + "".join(
                    f"{(timestamp - timedelta(minutes=5 - index)).isoformat()},TEST,{price}\n"
                    for index, price in enumerate((10, 11, 12, 13, 14, 15))
                ),
                encoding="utf-8",
            )
            bars = load_bars(settings.csv_path)["TEST"]
            for bar in bars[:4]:
                record_prediction(settings.database_path, bar, direction=1)
            performance = run_performance_cycle(settings)
            self.assertEqual(performance.evaluated, 4)
            self.assertEqual(performance.pending, 2)
            self.assertEqual(prediction_metrics(settings.database_path)[:2], (4, 4))

    def test_market_report_writes_to_its_own_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = settings_for(temporary)
            settings.csv_path.write_text(
                "timestamp,symbol,close\n"
                f"{datetime.now(timezone.utc).isoformat()},TEST,100\n",
                encoding="utf-8",
            )
            report = run_market_report(
                settings,
                now=datetime.now(timezone.utc),
            )
            self.assertEqual(report.parent, settings.report_dir)
            self.assertIn("TEST", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
