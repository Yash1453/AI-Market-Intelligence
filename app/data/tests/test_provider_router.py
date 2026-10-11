"""Mocked tests for Twelve Data, yfinance, Binance, and asset routing."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.data.adapters.base import EmptyDataError, MarketData, ProviderError
from app.data.adapters.binance_provider import BinanceProvider
from app.data.adapters.router import ProviderRouter
from app.data.adapters.twelve_data import TwelveDataProvider
from app.data.adapters.yfinance_provider import YFinanceProvider


class FakeHTTPResponse:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def json(self) -> object:
        return self.payload


class FakeProvider:
    def __init__(
        self,
        records: list[MarketData] | None = None,
        error: ProviderError | None = None,
    ) -> None:
        self.records = records or []
        self.error = error
        self.calls: list[str] = []

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "daily",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketData]:
        self.calls.append(symbol)
        if self.error:
            raise self.error
        return self.records

    def get_latest_data(self, symbols: list[str], timeframe: str = "daily") -> dict[str, MarketData]:
        return {}

    def get_asset_metadata(self, symbol: str) -> dict[str, object]:
        if self.error:
            raise self.error
        return {"symbol": symbol}


class ProviderRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_file = self.root / ".env"
        self.env_file.write_text("TWELVE_DATA_API_KEY=hidden-test-key\n", encoding="utf-8")
        self.record = MarketData(
            timestamp=datetime(2026, 10, 2, tzinfo=timezone.utc),
            symbol="AAPL",
            exchange="NASDAQ",
            timeframe="daily",
            open=10,
            high=12,
            low=9,
            close=11,
            volume=100,
            source="test",
        )

    def test_router_uses_twelve_data_first_for_stock(self) -> None:
        primary = FakeProvider([self.record])
        fallback = FakeProvider()
        router = ProviderRouter(
            {"AAPL": "stock"},
            {"twelve_data": primary, "yfinance": fallback},
        )
        self.assertEqual(router.get_historical_data("AAPL"), [self.record])
        self.assertEqual(primary.calls, ["AAPL"])
        self.assertEqual(fallback.calls, [])

    def test_router_falls_back_to_yfinance_for_stock(self) -> None:
        primary = FakeProvider(error=ProviderError("twelve data unavailable"))
        fallback = FakeProvider([self.record])
        router = ProviderRouter(
            {"AAPL": "stock"},
            {"twelve_data": primary, "yfinance": fallback},
        )
        self.assertEqual(router.get_historical_data("AAPL"), [self.record])
        self.assertEqual(fallback.calls, ["AAPL"])

    def test_router_falls_back_to_binance_for_crypto(self) -> None:
        primary = FakeProvider(error=EmptyDataError("not supported"))
        fallback = FakeProvider([self.record])
        router = ProviderRouter(
            {"BTC/USDT": "crypto"},
            {"twelve_data": primary, "binance": fallback},
        )
        self.assertEqual(router.get_historical_data("BTC/USDT"), [self.record])
        self.assertEqual(fallback.calls, ["BTC/USDT"])

    def test_forex_route_does_not_fallback_to_stock_or_crypto(self) -> None:
        primary = FakeProvider(error=ProviderError("no Twelve Data key"))
        router = ProviderRouter(
            {"EUR/USD": "fx"},
            {
                "twelve_data": primary,
                "yfinance": FakeProvider([self.record]),
                "binance": FakeProvider([self.record]),
            },
        )
        with self.assertRaises(ProviderError):
            router.get_historical_data("EUR/USD")

    def test_twelve_data_normalizes_ohlcv_without_logging_key(self) -> None:
        provider = TwelveDataProvider(
            self.root / "twelve.sqlite3",
            request_delay=0,
        )
        with (
            patch("app.data.adapters.secrets.ROOT", self.root),
            patch(
                "app.data.adapters.twelve_data.requests.get",
                return_value=FakeHTTPResponse(
                    {
                        "meta": {"exchange": "NASDAQ", "exchange_timezone": "UTC"},
                        "values": [
                            {
                                "datetime": "2026-10-02 00:00:00",
                                "open": "10",
                                "high": "12",
                                "low": "9",
                                "close": "11",
                                "volume": "100",
                            }
                        ],
                    }
                ),
            ) as request,
            self.assertLogs("app.data.adapters.twelve_data", level="INFO") as logs,
        ):
            records = provider.get_historical_data("AAPL", "daily")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source, "twelve_data")
        self.assertEqual(records[0].exchange, "NASDAQ")
        self.assertNotIn("hidden-test-key", "\n".join(logs.output))
        self.assertEqual(request.call_args.kwargs["params"]["interval"], "1day")

    def test_binance_normalizes_duplicate_crypto_klines(self) -> None:
        provider = BinanceProvider(self.root / "binance.sqlite3")
        candle = [1790899200000, "10", "12", "9", "11", "100"]
        with patch(
            "app.data.adapters.binance_provider.requests.get",
            return_value=FakeHTTPResponse([candle, candle]),
        ) as request:
            records = provider.get_historical_data("BTC/USDT", "daily")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].symbol, "BTC/USDT")
        self.assertEqual(records[0].source, "binance")
        self.assertEqual(records[0].exchange, "BINANCE")
        self.assertEqual(request.call_args.kwargs["params"]["symbol"], "BTCUSDT")

    def test_yfinance_normalizes_mocked_yahoo_chart_response(self) -> None:
        timestamp = int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp())
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"exchangeName": "NASDAQ"},
                        "timestamp": [timestamp],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [10],
                                    "high": [12],
                                    "low": [9],
                                    "close": [11],
                                    "volume": [100],
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }
        provider = YFinanceProvider(self.root / "yfinance.sqlite3")
        with patch(
            "app.data.adapters.yfinance_provider.requests.get",
            return_value=FakeHTTPResponse(payload),
        ) as request:
            records = provider.get_historical_data("AAPL", "daily")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source, "yfinance")
        self.assertEqual(records[0].close, 11)
        self.assertEqual(request.call_args.kwargs["params"]["interval"], "1d")


if __name__ == "__main__":
    unittest.main()
