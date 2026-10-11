"""One-shot live Alpha Vantage connectivity and data-quality check."""

from __future__ import annotations

import logging
import sys
from urllib.parse import urlsplit

from app.data.adapters.alpha_vantage import AlphaVantageProvider
from app.data.adapters.base import (
    MissingAPIKeyError,
    ProviderError,
)
from app.data.quality import data_quality_percent
from market_bot.config import Settings, load_settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings: Settings | None = None
    try:
        settings = load_settings()
        provider = AlphaVantageProvider(
            database_path=settings.database_path,
            base_url=settings.alpha_vantage_base_url,
            cache_minutes=settings.data_cache_minutes,
            request_delay=settings.api_request_delay,
        )
        symbol = settings.alpha_vantage_test_symbol
        timeframe = settings.alpha_vantage_test_timeframe
        records = provider.get_historical_data(symbol, timeframe)
    except MissingAPIKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ProviderError, OSError, ValueError) as exc:
        print(f"Alpha Vantage provider test failed: {exc}", file=sys.stderr)
        print("Provider: Alpha Vantage", file=sys.stderr)
        endpoint = getattr(exc, "endpoint", None)
        if endpoint is None:
            base_url = (
                settings.alpha_vantage_base_url
                if settings is not None
                else "https://www.alphavantage.co/query"
            )
            parsed = urlsplit(base_url)
            endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        print(f"Endpoint: {endpoint}", file=sys.stderr)
        request_params = getattr(exc, "request_params", {})
        for name in ("function", "symbol", "interval", "outputsize", "datatype"):
            value = request_params.get(name, "not requested")
            print(f"{name}: {value}", file=sys.stderr)
        print(f"Response type: {getattr(exc, 'response_type', 'unavailable')}", file=sys.stderr)
        print(
            f"Response keys: {', '.join(getattr(exc, 'response_keys', ())) or '(none)'}",
            file=sys.stderr,
        )
        for name, message in getattr(exc, "response_messages", {}).items():
            print(f"{name}: {message}", file=sys.stderr)
        print("Records: unavailable", file=sys.stderr)
        print("Latest timestamp: unavailable", file=sys.stderr)
        print("Latest close: unavailable", file=sys.stderr)
        print("Data quality: unavailable", file=sys.stderr)
        return 1

    latest = records[-1]
    print("Provider: Alpha Vantage")
    print(f"Endpoint: {provider.endpoint}")
    print(f"Symbol: {symbol}")
    print(f"Timeframe: {timeframe}")
    print(f"function: {provider.last_request_params['function']}")
    print(f"interval: {provider.last_request_params.get('interval', 'not applicable')}")
    print(f"outputsize: {provider.last_request_params['outputsize']}")
    print(f"datatype: {provider.last_request_params['datatype']}")
    print(f"Response type: {provider.last_response_type}")
    print(f"Response keys: {', '.join(provider.last_response_keys)}")
    print(f"Records: {len(records)}")
    print(f"Latest timestamp: {latest.timestamp.isoformat()}")
    print(f"Latest close: {latest.close:g}")
    print(f"Data quality: {data_quality_percent(records):.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
