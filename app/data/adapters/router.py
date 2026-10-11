"""Asset-aware provider router with bounded provider fallback."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from app.data.adapters.base import DataProvider, MarketData, ProviderError
from app.data.adapters.binance_provider import BinanceProvider
from app.data.adapters.twelve_data import TwelveDataProvider
from app.data.adapters.yfinance_provider import YFinanceProvider

LOGGER = logging.getLogger(__name__)


class ProviderRouter(DataProvider):
    """Try Twelve Data first, then the configured asset-class fallback."""

    def __init__(
        self,
        asset_types: dict[str, str],
        providers: dict[str, DataProvider],
        symbol_defaults: tuple[str, ...] = (),
    ) -> None:
        self.asset_types = {symbol.upper(): kind.lower() for symbol, kind in asset_types.items()}
        self.providers = providers
        self.symbol_defaults = tuple(symbol.upper() for symbol in symbol_defaults)

    @classmethod
    def configured(
        cls,
        database_path: Path,
        asset_types: dict[str, str],
        symbols: tuple[str, ...],
        twelve_data_base_url: str = "https://api.twelvedata.com",
        binance_base_url: str = "https://api.binance.com",
        cache_minutes: int = 15,
        request_delay: float = 1.0,
        yfinance_enabled: bool = True,
    ) -> ProviderRouter:
        providers: dict[str, DataProvider] = {
            "twelve_data": TwelveDataProvider(
                database_path=database_path,
                base_url=twelve_data_base_url,
                cache_minutes=cache_minutes,
                request_delay=request_delay,
            ),
            "binance": BinanceProvider(
                database_path=database_path,
                base_url=binance_base_url,
                cache_minutes=cache_minutes,
            ),
        }
        if yfinance_enabled:
            providers["yfinance"] = YFinanceProvider(
                database_path=database_path,
                cache_minutes=cache_minutes,
            )
        return cls(asset_types, providers, symbols)

    def _route(self, symbol: str) -> tuple[str, ...]:
        kind = self.asset_types.get(symbol.upper(), "stock")
        fallback_by_type: dict[str, tuple[str, ...]] = {
            "stock": ("yfinance",),
            "fx": (),
            "crypto": ("binance",),
        }
        if kind not in fallback_by_type:
            raise ValueError(f"Unsupported asset type for {symbol}: {kind}")
        return ("twelve_data", *fallback_by_type[kind])

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "daily",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketData]:
        errors: list[str] = []
        for provider_name in self._route(symbol):
            provider = self.providers.get(provider_name)
            if provider is None:
                continue
            try:
                records = provider.get_historical_data(symbol, timeframe, start, end)
                LOGGER.info(
                    "provider_router symbol=%s timeframe=%s selected_provider=%s "
                    "request_status=success records=%d",
                    symbol,
                    timeframe,
                    provider_name,
                    len(records),
                )
                return records
            except ProviderError as exc:
                errors.append(f"{provider_name}: {exc}")
                LOGGER.warning(
                    "provider_router symbol=%s timeframe=%s provider=%s request_status=fallback "
                    "error=%s",
                    symbol,
                    timeframe,
                    provider_name,
                    type(exc).__name__,
                )
        raise ProviderError(
            f"No configured market-data provider succeeded for {symbol} ({timeframe}): "
            + "; ".join(errors or ["no eligible fallback provider is configured"])
        )

    def get_latest_data(
        self,
        symbols: list[str],
        timeframe: str = "daily",
    ) -> dict[str, MarketData]:
        latest: dict[str, MarketData] = {}
        for symbol in symbols:
            records = self.get_historical_data(symbol, timeframe)
            if records:
                latest[symbol.strip().upper()] = records[-1]
        return latest

    def get_asset_metadata(self, symbol: str) -> dict[str, Any]:
        errors: list[str] = []
        for provider_name in self._route(symbol):
            provider = self.providers.get(provider_name)
            if provider is None:
                continue
            try:
                metadata = provider.get_asset_metadata(symbol)
                return {"provider": provider_name, **metadata}
            except ProviderError as exc:
                errors.append(f"{provider_name}: {exc}")
        raise ProviderError(
            f"No configured provider returned metadata for {symbol}: "
            + "; ".join(errors or ["no eligible fallback provider is configured"])
        )
