"""Provider construction and common multi-symbol loading helpers."""

from __future__ import annotations

from app.data.adapters.alpha_vantage import AlphaVantageProvider
from app.data.adapters.base import DataProvider, MarketData
from app.data.adapters.csv_provider import CSVProvider
from app.data.adapters.router import ProviderRouter
from market_bot.config import Settings


def create_provider(settings: Settings) -> DataProvider:
    if settings.data_provider == "csv":
        return CSVProvider(settings.csv_path, settings.timeframe)
    if settings.data_provider == "alpha_vantage":
        return AlphaVantageProvider(
            database_path=settings.database_path,
            base_url=settings.alpha_vantage_base_url,
            cache_minutes=settings.data_cache_minutes,
            request_delay=settings.api_request_delay,
        )
    if settings.data_provider == "router":
        return ProviderRouter.configured(
            database_path=settings.database_path,
            asset_types=settings.asset_types or {},
            symbols=settings.asset_symbols,
            twelve_data_base_url=settings.twelve_data_base_url,
            binance_base_url=settings.binance_base_url,
            cache_minutes=settings.data_cache_minutes,
            request_delay=settings.api_request_delay,
            yfinance_enabled=settings.yfinance_enabled,
        )
    raise ValueError(f"Unsupported data provider: {settings.data_provider}")


def load_histories(
    provider: DataProvider,
    symbols: list[str] | tuple[str, ...] | None,
    timeframe: str,
) -> dict[str, list[MarketData]]:
    selected = list(symbols or ())
    if isinstance(provider, CSVProvider) and symbols is None:
        selected = provider.get_available_symbols()
    histories: dict[str, list[MarketData]] = {}
    for symbol in selected:
        bars = provider.get_historical_data(symbol, timeframe)
        if bars:
            histories[symbol.upper()] = bars
    return histories
