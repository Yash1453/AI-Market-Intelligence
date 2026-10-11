"""Configuration isolated to the market-intelligence bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_local_env() -> None:
    """Load market_bot/.env without reading or changing the job agent's .env."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line_number, raw_line in enumerate(env_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"Invalid market bot .env entry on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _path_setting(name: str, default: Path) -> Path:
    value = os.getenv(name)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _unit_interval(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be between 0 and 1") from exc
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative number") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def _symbols(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    symbols = tuple(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    if not symbols:
        raise ValueError(f"{name} must contain at least one symbol")
    return symbols


def _asset_types(name: str, symbols: tuple[str, ...]) -> dict[str, str]:
    value = os.getenv(name, "")
    asset_types: dict[str, str] = {}
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        symbol, separator, asset_type = entry.partition(":")
        normalized_symbol = symbol.strip().upper()
        normalized_type = asset_type.strip().lower()
        if not separator or not normalized_symbol or normalized_type not in {"stock", "fx", "crypto"}:
            raise ValueError(
                f"{name} entries must use SYMBOL:stock, SYMBOL:fx, or SYMBOL:crypto"
            )
        asset_types[normalized_symbol] = normalized_type
    for symbol in symbols:
        asset_types.setdefault(symbol, "stock")
    return asset_types


@dataclass(frozen=True)
class Settings:
    csv_path: Path
    database_path: Path
    report_dir: Path
    fast_window: int
    slow_window: int
    hold_bars: int
    max_data_age_minutes: int
    signal_interval_seconds: int
    performance_interval_seconds: int
    report_interval_seconds: int
    monitor_min_predictions: int
    monitor_min_hit_rate: float
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    data_provider: str = "csv"
    asset_symbols: tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "TSLA", "AMZN")
    scan_symbols: tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "TSLA", "AMZN")
    timeframe: str = "5min"
    max_symbols_per_scan: int = 5
    api_request_delay: float = 12.0
    data_cache_minutes: int = 15
    alpha_vantage_base_url: str = "https://www.alphavantage.co/query"
    alpha_vantage_test_symbol: str = "AAPL"
    alpha_vantage_test_timeframe: str = "daily"
    asset_types: dict[str, str] | None = None
    twelve_data_base_url: str = "https://api.twelvedata.com"
    yfinance_enabled: bool = True
    binance_base_url: str = "https://api.binance.com"


def load_settings() -> Settings:
    _load_local_env()
    data_provider = os.getenv("DATA_PROVIDER", "csv").strip().lower()
    if data_provider not in {"csv", "alpha_vantage", "router"}:
        raise ValueError("DATA_PROVIDER must be csv, alpha_vantage, or router")
    asset_symbols = _symbols("ASSET_SYMBOLS", ("AAPL", "MSFT", "NVDA", "TSLA", "AMZN"))
    scan_symbols = _symbols("SCAN_SYMBOLS", asset_symbols)
    asset_types = _asset_types("ASSET_TYPES", asset_symbols)
    timeframe = os.getenv("MARKET_TIMEFRAME", "5min").strip().lower()
    if timeframe not in {"5min", "15min", "30min", "60min", "daily"}:
        raise ValueError("MARKET_TIMEFRAME must be 5min, 15min, 30min, 60min, or daily")
    test_timeframe = os.getenv("ALPHA_VANTAGE_TEST_TIMEFRAME", "daily").strip().lower()
    if test_timeframe not in {"5min", "15min", "30min", "60min", "daily"}:
        raise ValueError(
            "ALPHA_VANTAGE_TEST_TIMEFRAME must be 5min, 15min, 30min, 60min, or daily"
        )
    test_symbol = os.getenv("ALPHA_VANTAGE_TEST_SYMBOL", "AAPL").strip().upper()
    if not test_symbol:
        raise ValueError("ALPHA_VANTAGE_TEST_SYMBOL cannot be empty")
    fast_window = _positive_int("MARKET_FAST_WINDOW", 5)
    slow_window = _positive_int("MARKET_SLOW_WINDOW", 20)
    if fast_window >= slow_window:
        raise ValueError("MARKET_FAST_WINDOW must be less than MARKET_SLOW_WINDOW")

    token = os.getenv("MARKET_TELEGRAM_BOT_TOKEN", "").strip() or None
    chat_id = os.getenv("MARKET_TELEGRAM_CHAT_ID", "").strip() or None
    if (token is None) != (chat_id is None):
        raise ValueError(
            "Set both MARKET_TELEGRAM_BOT_TOKEN and MARKET_TELEGRAM_CHAT_ID, or leave both unset"
        )

    return Settings(
        csv_path=_path_setting("MARKET_CSV_PATH", ROOT / "data" / "market_prices.csv"),
        database_path=_path_setting("MARKET_DATABASE_PATH", ROOT / "data" / "market_bot.sqlite3"),
        report_dir=_path_setting("MARKET_REPORT_DIR", ROOT / "reports"),
        fast_window=fast_window,
        slow_window=slow_window,
        hold_bars=_positive_int("MARKET_PAPER_HOLD_BARS", 5),
        max_data_age_minutes=_positive_int("MARKET_MAX_DATA_AGE_MINUTES", 180),
        signal_interval_seconds=_positive_int("MARKET_SIGNAL_INTERVAL_SECONDS", 60),
        performance_interval_seconds=_positive_int("MARKET_PERFORMANCE_INTERVAL_SECONDS", 300),
        report_interval_seconds=_positive_int("MARKET_REPORT_INTERVAL_SECONDS", 3600),
        monitor_min_predictions=_positive_int("MARKET_MONITOR_MIN_PREDICTIONS", 20),
        monitor_min_hit_rate=_unit_interval("MARKET_MONITOR_MIN_HIT_RATE", 0.45),
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        data_provider=data_provider,
        asset_symbols=asset_symbols,
        scan_symbols=scan_symbols,
        timeframe=timeframe,
        max_symbols_per_scan=_positive_int("MAX_SYMBOLS_PER_SCAN", 5),
        api_request_delay=_non_negative_float("API_REQUEST_DELAY", 12.0),
        data_cache_minutes=_positive_int("DATA_CACHE_MINUTES", 15),
        alpha_vantage_base_url=os.getenv(
            "ALPHA_VANTAGE_BASE_URL",
            "https://www.alphavantage.co/query",
        ).strip(),
        alpha_vantage_test_symbol=test_symbol,
        alpha_vantage_test_timeframe=test_timeframe,
        asset_types=asset_types,
        twelve_data_base_url=os.getenv(
            "TWELVE_DATA_BASE_URL",
            "https://api.twelvedata.com",
        ).strip(),
        yfinance_enabled=os.getenv("YFINANCE_ENABLED", "true").strip().lower() == "true",
        binance_base_url=os.getenv("BINANCE_BASE_URL", "https://api.binance.com").strip(),
    )
