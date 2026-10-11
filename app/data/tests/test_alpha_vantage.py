"""Mocked Alpha Vantage adapter and CSV-provider tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from app.data.adapters.alpha_vantage import AlphaVantageProvider, read_api_key
from app.data.adapters.base import (
    EmptyDataError,
    InvalidAPIKeyError,
    MarketData,
    MalformedResponseError,
    MissingAPIKeyError,
    NetworkError,
    ProviderDiagnosticError,
    ProviderOutageError,
    RateLimitError,
)
from app.data.adapters.csv_provider import CSVProvider
from app.data.quality import data_quality_percent
from app.data.provider import create_provider
from market_bot.config import Settings
from market_bot.store import cache_market_data, get_cached_market_data


class FakeResponse:
    status = 200

    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def series_payload(
    series: object | None = None,
    timeframe: str = "5min",
) -> dict[str, object]:
    timestamps = (
        ("2026-10-02", "2026-10-01")
        if timeframe == "daily"
        else ("2026-10-02 16:00:00", "2026-10-02 15:55:00")
    )
    return {
        "Meta Data": {
            "2. Symbol": "AAPL",
            "6. Time Zone": "US/Eastern",
        },
        {
            "5min": "Time Series (5min)",
            "15min": "Time Series (15min)",
            "30min": "Time Series (30min)",
            "60min": "Time Series (60min)",
            "daily": "Time Series (Daily)",
        }[timeframe]: series
        if series is not None
        else {
            timestamps[0]: {
                "1. open": "100",
                "2. high": "105",
                "3. low": "99",
                "4. close": "104",
                "5. volume": "1200",
            },
            timestamps[1]: {
                "1. open": "98",
                "2. high": "101",
                "3. low": "97",
                "4. close": "100",
                "5. volume": "800",
            },
        },
    }


class AlphaVantageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env_file = self.root / ".env"
        self.env_file.write_text("ALPHA_VANTAGE_API_KEY=test-key\n", encoding="utf-8")

    def provider(self, **options: object) -> AlphaVantageProvider:
        defaults: dict[str, object] = {
            "database_path": self.root / "market.sqlite3",
            "request_delay": 0,
            "sleeper": lambda _delay: None,
        }
        defaults.update(options)
        with patch(
            "app.data.adapters.alpha_vantage.ALPHA_VANTAGE_ENV_FILE",
            self.env_file,
        ):
            return AlphaVantageProvider(**defaults)  # type: ignore[arg-type]

    def test_success_normalizes_all_required_fields(self) -> None:
        provider = self.provider()
        with (
            patch(
                "app.data.adapters.alpha_vantage.urlopen",
                return_value=FakeResponse(series_payload()),
            ),
            self.assertLogs("app.data.adapters.alpha_vantage", level="INFO") as logs,
        ):
            records = provider.get_historical_data("aapl", "5min")
        self.assertEqual(len(records), 2)
        record = records[-1]
        self.assertEqual(
            set(record.__dataclass_fields__),
            {
                "timestamp",
                "symbol",
                "exchange",
                "timeframe",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "source",
            },
        )
        self.assertEqual(record.symbol, "AAPL")
        self.assertEqual(record.timeframe, "5min")
        self.assertEqual(record.source, "alpha_vantage")
        self.assertEqual(record.timestamp, datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(record.close, 104)
        self.assertNotIn("test-key", "\n".join(logs.output))

    def test_invalid_api_key_is_reported_without_retries(self) -> None:
        provider = self.provider(max_retries=3)
        response = {"Information": "The parameter apikey is invalid or missing."}
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse(response),
        ) as request:
            with self.assertRaises(InvalidAPIKeyError):
                provider.get_historical_data("AAPL", "5min")
        request.assert_called_once()

    def test_informational_response_exposes_safe_exact_message_and_request_diagnostics(self) -> None:
        provider = self.provider()
        message = "Your key test-key is restricted: https://example.test/query?apikey=test-key"
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse({"Information": message}),
        ) as request:
            with self.assertRaises(ProviderDiagnosticError) as caught:
                provider.get_historical_data("AAPL", "daily")
        error = caught.exception
        self.assertEqual(error.endpoint, "https://www.alphavantage.co/query")
        self.assertEqual(error.request_params["function"], "TIME_SERIES_DAILY")
        self.assertEqual(error.request_params["symbol"], "AAPL")
        self.assertEqual(error.request_params["outputsize"], "compact")
        self.assertEqual(error.request_params["datatype"], "json")
        self.assertEqual(error.response_keys, ("Information",))
        self.assertIn("[REDACTED]", error.response_messages["Information"])
        self.assertNotIn("test-key", str(error))
        requested_url = request.call_args.args[0].full_url
        self.assertIn("function=TIME_SERIES_DAILY", requested_url)
        self.assertIn("symbol=AAPL", requested_url)
        self.assertIn("outputsize=compact", requested_url)
        self.assertIn("datatype=json", requested_url)

    def test_non_credential_provider_urls_remain_in_provider_message(self) -> None:
        provider = self.provider()
        message = (
            "This is a premium endpoint. Subscribe at "
            "https://www.alphavantage.co/premium/ to unlock access."
        )
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse({"Information": message}),
        ):
            with self.assertRaises(ProviderDiagnosticError) as caught:
                provider.get_historical_data("AAPL", "5min")
        self.assertEqual(caught.exception.response_messages["Information"], message)

    def test_rate_limit_uses_bounded_exponential_backoff(self) -> None:
        sleeps: list[float] = []
        provider = self.provider(max_retries=2, sleeper=sleeps.append)
        rate_limit = {"Note": "Our API call frequency is limited."}
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse(rate_limit),
        ) as request:
            with self.assertRaises(RateLimitError):
                provider.get_historical_data("AAPL", "5min")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_historical_cache_prevents_repeated_provider_requests(self) -> None:
        provider = self.provider()
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse(series_payload()),
        ) as request:
            first = provider.get_historical_data("AAPL", "5min")
            second = provider.get_historical_data("AAPL", "5min")
        self.assertEqual(first, second)
        request.assert_called_once()

    def test_provider_outage_is_retried_a_finite_number_of_times(self) -> None:
        provider = self.provider(max_retries=1)
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse({"Information": "Service temporarily unavailable."}),
        ) as request:
            with self.assertRaises(ProviderOutageError):
                provider.get_historical_data("AAPL", "5min")
        self.assertEqual(request.call_count, 2)

    def test_malformed_response_is_rejected(self) -> None:
        provider = self.provider()
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse({"Time Series (5min)": ["not", "a", "mapping"]}),
        ):
            with self.assertRaises(MalformedResponseError):
                provider.get_historical_data("AAPL", "5min")

    def test_empty_response_is_reported(self) -> None:
        provider = self.provider()
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse({"Meta Data": {"2. Symbol": "AAPL"}}),
        ):
            with self.assertRaises(EmptyDataError):
                provider.get_historical_data("AAPL", "5min")

    def test_empty_http_body_is_reported(self) -> None:
        class EmptyResponse(FakeResponse):
            def read(self) -> bytes:
                return b""

        provider = self.provider()
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=EmptyResponse({}),
        ):
            with self.assertRaises(EmptyDataError):
                provider.get_historical_data("AAPL", "5min")

    def test_network_timeout_has_bounded_retries(self) -> None:
        provider = self.provider(max_retries=2)
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            side_effect=TimeoutError(),
        ) as request:
            with self.assertRaises(NetworkError):
                provider.get_historical_data("AAPL", "5min")
        self.assertEqual(request.call_count, 3)

    def test_duplicate_candles_are_not_duplicated_in_local_cache(self) -> None:
        bars = AlphaVantageProvider._parse_series(series_payload(), "AAPL", "5min")
        path = self.root / "market.sqlite3"
        cache_market_data(path, bars)
        cache_market_data(path, bars)
        cached = get_cached_market_data(
            path,
            "AAPL",
            "5min",
            datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(cached), 2)

    def test_missing_candles_reduce_quality_score(self) -> None:
        records = [
            MarketData(
                timestamp=datetime(2026, 10, 2, hour=13, minute=0, tzinfo=timezone.utc),
                symbol="AAPL",
                exchange="",
                timeframe="5min",
                open=1,
                high=1,
                low=1,
                close=1,
                volume=0,
                source="test",
            ),
            MarketData(
                timestamp=datetime(2026, 10, 2, hour=13, minute=15, tzinfo=timezone.utc),
                symbol="AAPL",
                exchange="",
                timeframe="5min",
                open=1,
                high=1,
                low=1,
                close=1,
                volume=0,
                source="test",
            ),
        ]
        self.assertEqual(data_quality_percent(records), 50.0)

    def test_ohlc_validation_rejects_inconsistent_values(self) -> None:
        provider = self.provider()
        payload = series_payload()
        candles = payload["Time Series (5min)"]
        candles["2026-10-02 16:00:00"]["2. high"] = "100"
        with patch(
            "app.data.adapters.alpha_vantage.urlopen",
            return_value=FakeResponse(payload),
        ):
            with self.assertRaises(MalformedResponseError):
                provider.get_historical_data("AAPL", "5min")

    def test_csv_provider_returns_same_model(self) -> None:
        csv_path = self.root / "prices.csv"
        csv_path.write_text(
            "timestamp,symbol,exchange,timeframe,open,high,low,close,volume\n"
            "2026-10-02T13:00:00Z,AAPL,NASDAQ,5min,100,105,99,104,1200\n",
            encoding="utf-8",
        )
        records = CSVProvider(csv_path).get_historical_data("AAPL", "5min")
        self.assertEqual(len(records), 1)
        self.assertIsInstance(records[0], MarketData)
        self.assertEqual(records[0].source, "csv")
        self.assertEqual(records[0].exchange, "NASDAQ")

    def test_all_configured_intervals_map_to_alpha_vantage_functions(self) -> None:
        for timeframe in ("5min", "15min", "30min", "60min", "daily"):
            with self.subTest(timeframe=timeframe):
                provider = self.provider(database_path=self.root / f"{timeframe}.sqlite3")
                payload = series_payload(timeframe=timeframe)
                with patch(
                    "app.data.adapters.alpha_vantage.urlopen",
                    return_value=FakeResponse(payload),
                ) as request:
                    records = provider.get_historical_data("AAPL", timeframe)
                self.assertEqual(len(records), 2)
                request_object = request.call_args.args[0]
                query = parse_qs(urlsplit(request_object.full_url).query)
                if timeframe == "daily":
                    self.assertEqual(query["function"], ["TIME_SERIES_DAILY"])
                    self.assertNotIn("interval", query)
                else:
                    self.assertEqual(query["function"], ["TIME_SERIES_INTRADAY"])
                    self.assertEqual(query["interval"], [timeframe])
                self.assertEqual(query["datatype"], ["json"])
                self.assertEqual(query["outputsize"], ["compact"])

    def test_api_key_is_read_only_from_local_env_file(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("ALPHA_VANTAGE_API_KEY=file-secret\n", encoding="utf-8")
        with patch("app.data.adapters.alpha_vantage.ALPHA_VANTAGE_ENV_FILE", env_file):
            self.assertEqual(read_api_key(), "file-secret")
        env_file.write_text("ALPHA_VANTAGE_API_KEY=\n", encoding="utf-8")
        with (
            patch("app.data.adapters.alpha_vantage.ALPHA_VANTAGE_ENV_FILE", env_file),
            patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "process-secret"}),
        ):
            with self.assertRaises(MissingAPIKeyError):
                read_api_key()

    def test_local_env_loads_provider_settings_without_exposing_key(self) -> None:
        from market_bot import config

        self.env_file.write_text(
            "DATA_PROVIDER=alpha_vantage\n"
            "SCAN_SYMBOLS=AAPL,MSFT\n"
            "MARKET_TIMEFRAME=15min\n",
            encoding="utf-8",
        )
        with (
            patch("market_bot.config.ROOT", self.root),
            patch.dict(os.environ, {}, clear=True),
        ):
            settings = config.load_settings()
        self.assertEqual(settings.data_provider, "alpha_vantage")
        self.assertEqual(settings.scan_symbols, ("AAPL", "MSFT"))
        self.assertEqual(settings.timeframe, "15min")

    def test_alpha_provider_fails_clearly_when_local_key_is_missing(self) -> None:
        self.env_file.unlink()
        settings = Settings(
            csv_path=self.root / "prices.csv",
            database_path=self.root / "market.sqlite3",
            report_dir=self.root / "reports",
            fast_window=2,
            slow_window=3,
            hold_bars=2,
            max_data_age_minutes=60,
            signal_interval_seconds=60,
            performance_interval_seconds=300,
            report_interval_seconds=3600,
            monitor_min_predictions=3,
            monitor_min_hit_rate=0.45,
            telegram_bot_token=None,
            telegram_chat_id=None,
            data_provider="alpha_vantage",
        )
        with patch(
            "app.data.adapters.alpha_vantage.ALPHA_VANTAGE_ENV_FILE",
            self.env_file,
        ):
            with self.assertRaisesRegex(
                MissingAPIKeyError,
                r"Alpha Vantage API key is missing\.\nAdd ALPHA_VANTAGE_API_KEY to \.env\.",
            ):
                create_provider(settings)


if __name__ == "__main__":
    unittest.main()
