"""Bounded, cached Alpha Vantage adapter."""

from __future__ import annotations

import json
import logging
import math
import re
import socket
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.data.adapters.base import (
    DataProvider,
    EmptyDataError,
    InvalidAPIKeyError,
    MarketData,
    MalformedResponseError,
    MissingAPIKeyError,
    NetworkError,
    ProviderError,
    ProviderDiagnosticError,
    ProviderOutageError,
    RateLimitError,
)
from market_bot.config import ROOT as MARKET_BOT_ROOT
from market_bot.store import (
    cache_market_data,
    get_cached_market_data,
)

LOGGER = logging.getLogger(__name__)
ALPHA_VANTAGE_ENV_FILE = MARKET_BOT_ROOT / ".env"
API_KEY_ERROR_MESSAGE = (
    "Alpha Vantage API key is missing.\n"
    "Add ALPHA_VANTAGE_API_KEY to .env."
)
SUPPORTED_TIMEFRAMES = {"5min", "15min", "30min", "60min", "daily"}
_TIME_SERIES_LABELS = {
    "5min": "Time Series (5min)",
    "15min": "Time Series (15min)",
    "30min": "Time Series (30min)",
    "60min": "Time Series (60min)",
    "daily": "Time Series (Daily)",
}
_RETRYABLE_ERRORS = (RateLimitError, ProviderOutageError, NetworkError)


def read_api_key() -> str:
    """Read the API key only from market_bot/.env, never the process environment."""
    env_file = ALPHA_VANTAGE_ENV_FILE
    if not env_file.is_file():
        raise MissingAPIKeyError(API_KEY_ERROR_MESSAGE)
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if key.strip() != "ALPHA_VANTAGE_API_KEY":
            continue
        if not separator:
            break
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            return value
        break
    raise MissingAPIKeyError(API_KEY_ERROR_MESSAGE)


def _timezone_for_market(name: str, local_time: datetime) -> tzinfo:
    normalized = name.strip().lower()
    if normalized in {"utc", "etc/utc", "gmt"}:
        return timezone.utc
    us_offsets = {
        "us/eastern": (-5, -4),
        "america/new_york": (-5, -4),
        "us/central": (-6, -5),
        "america/chicago": (-6, -5),
        "us/mountain": (-7, -6),
        "america/denver": (-7, -6),
        "us/pacific": (-8, -7),
        "america/los_angeles": (-8, -7),
    }
    if normalized in us_offsets and local_time.year >= 2007:
        standard_offset, daylight_offset = us_offsets[normalized]
        first_march_sunday = 1 + (6 - datetime(local_time.year, 3, 1).weekday()) % 7
        second_march_sunday = first_march_sunday + 7
        first_november_sunday = 1 + (6 - datetime(local_time.year, 11, 1).weekday()) % 7
        dst_starts = datetime(local_time.year, 3, second_march_sunday, 2)
        dst_ends = datetime(local_time.year, 11, first_november_sunday, 2)
        if dst_starts <= local_time < dst_ends:
            return timezone(timedelta(hours=daylight_offset), name)
        return timezone(timedelta(hours=standard_offset), name)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise MalformedResponseError(
            "Alpha Vantage returned a timezone unavailable to this Python installation."
        ) from exc


class AlphaVantageProvider(DataProvider):
    def __init__(
        self,
        database_path: Path,
        base_url: str = "https://www.alphavantage.co/query",
        cache_minutes: int = 15,
        request_delay: float = 12.0,
        timeout: float = 15.0,
        max_retries: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if cache_minutes < 1:
            raise ValueError("DATA_CACHE_MINUTES must be positive")
        if request_delay < 0 or timeout <= 0 or max_retries < 0:
            raise ValueError("Invalid Alpha Vantage request settings")
        parsed_base_url = urlsplit(base_url)
        if (
            parsed_base_url.scheme != "https"
            or not parsed_base_url.netloc
            or parsed_base_url.query
            or parsed_base_url.fragment
        ):
            raise ValueError("ALPHA_VANTAGE_BASE_URL must be an absolute HTTPS URL")
        self.api_key = read_api_key()
        self.database_path = database_path
        self.base_url = base_url
        self.endpoint = f"{parsed_base_url.scheme}://{parsed_base_url.netloc}{parsed_base_url.path}"
        self.last_request_params: dict[str, str] = {}
        self.last_response_keys: tuple[str, ...] = ()
        self.last_response_type = "unavailable"
        self.cache_minutes = cache_minutes
        self.request_delay = request_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleeper
        self._last_request_at: float | None = None
        self._metadata_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _wait_for_request_slot(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.request_delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    def _safe_response_text(self, value: Any) -> str:
        text = str(value)
        for credential in {self.api_key, quote(self.api_key), quote_plus(self.api_key)}:
            text = text.replace(credential, "[REDACTED]")
        text = re.sub(r"(?i)(apikey\s*[=:]\s*)[^&\s,]+", r"\1[REDACTED]", text)

        def redact_credential_url(match: re.Match[str]) -> str:
            candidate = match.group(0)
            query_values = parse_qs(urlsplit(candidate.rstrip(".,;)]")).query)
            if "apikey" in {key.lower() for key in query_values.keys()}:
                return "[URL REDACTED]"
            credentials = (self.api_key, quote(self.api_key), quote_plus(self.api_key))
            if any(credential in candidate for credential in credentials):
                return "[URL REDACTED]"
            return candidate

        text = re.sub(r"https?://[^\s]+", redact_credential_url, text)
        return text

    def _error_from_payload(
        self,
        payload: dict[str, Any],
        params: dict[str, str],
    ) -> ProviderError | None:
        response_keys = sorted(str(key) for key in payload)
        response_messages = {
            field: self._safe_response_text(payload[field])
            for field in ("Information", "Note", "Error Message")
            if field in payload
        }
        diagnostic_message = self._format_response_diagnostic(
            response_keys,
            response_messages,
        )

        def with_diagnostics(error: ProviderError) -> ProviderError:
            setattr(error, "endpoint", self.endpoint)
            setattr(error, "request_params", dict(params))
            setattr(error, "response_keys", tuple(response_keys))
            setattr(error, "response_messages", dict(response_messages))
            setattr(error, "response_type", "JSON object")
            return error

        message = payload.get("Error Message")
        if message is not None:
            text = str(message).lower()
            if "api key" in text or "apikey" in text or "invalid key" in text:
                reason = "Alpha Vantage rejected the API key."
                return with_diagnostics(InvalidAPIKeyError(f"{reason} {diagnostic_message}"))
            elif "invalid api call" in text:
                reason = "Alpha Vantage rejected the requested function or symbol."
                return with_diagnostics(MalformedResponseError(f"{reason} {diagnostic_message}"))
            else:
                reason = "Alpha Vantage returned an API error."
                return with_diagnostics(MalformedResponseError(f"{reason} {diagnostic_message}"))

        for field in ("Note", "Information"):
            message = payload.get(field)
            if message is None:
                continue
            text = str(message).lower()
            if any(term in text for term in ("rate limit", "call frequency", "api call frequency")):
                reason = "Alpha Vantage rate limit reached."
                return with_diagnostics(RateLimitError(f"{reason} {diagnostic_message}"))
            elif any(term in text for term in ("api key", "apikey")) and any(
                term in text for term in ("invalid", "missing", "demo")
            ):
                reason = "Alpha Vantage rejected the API key."
                return with_diagnostics(InvalidAPIKeyError(f"{reason} {diagnostic_message}"))
            elif any(
                term in text
                for term in (
                    "temporarily unavailable",
                    "internal server error",
                    "server error",
                    "service unavailable",
                    "try again later",
                )
            ):
                reason = "Alpha Vantage is temporarily unavailable."
                return with_diagnostics(ProviderOutageError(f"{reason} {diagnostic_message}"))
            else:
                reason = "Alpha Vantage returned an informational response instead of time-series data."
                return ProviderDiagnosticError(
                    f"{reason} {diagnostic_message}",
                    endpoint=self.endpoint,
                    request_params=params,
                    response_keys=response_keys,
                    response_messages=response_messages,
                    response_type="JSON object",
                )
        return None

    @staticmethod
    def _format_response_diagnostic(
        response_keys: list[str],
        response_messages: dict[str, str],
    ) -> str:
        details = ", ".join(f"{field}: {message}" for field, message in response_messages.items())
        keys = ", ".join(response_keys) if response_keys else "(none)"
        return f"Response keys: [{keys}]." + (f" Provider message: {details}" if details else "")

    def _request_json(
        self,
        params: dict[str, str],
        symbol: str,
        timeframe: str,
    ) -> tuple[dict[str, Any], float]:
        self.last_request_params = dict(params)
        query = urlencode({**params, "apikey": self.api_key})
        request = Request(f"{self.base_url}?{query}", headers={"User-Agent": "MarketIntelligenceBot/1.0"})
        for attempt in range(self.max_retries + 1):
            self._wait_for_request_slot()
            started = perf_counter()
            status = "error"
            try:
                self._last_request_at = time.monotonic()
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    http_status = getattr(response, "status", 200)
                latency_ms = (perf_counter() - started) * 1000
                if not raw:
                    raise EmptyDataError("Alpha Vantage returned an empty response.")
                if http_status >= 500:
                    raise ProviderOutageError("Alpha Vantage service returned a server error.")
                if http_status >= 400:
                    raise MalformedResponseError(
                        f"Alpha Vantage returned HTTP status {http_status}."
                    )
                try:
                    decoded: object = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise MalformedResponseError("Alpha Vantage returned malformed JSON.") from exc
                decoded_mapping = cast(dict[object, object], decoded) if isinstance(decoded, dict) else None
                if decoded_mapping is None or not all(
                    isinstance(key, str) for key in decoded_mapping.keys()
                ):
                    raise MalformedResponseError("Alpha Vantage returned an unexpected JSON structure.")
                payload = cast(dict[str, Any], decoded)
                if not payload:
                    raise EmptyDataError("Alpha Vantage returned an empty response.")
                self.last_response_type = "JSON object"
                self.last_response_keys = tuple(sorted(str(key) for key in payload))
                LOGGER.info(
                    "provider=alpha_vantage symbol=%s timeframe=%s response_keys=%s",
                    symbol,
                    timeframe,
                    ",".join(self.last_response_keys),
                )
                provider_error = self._error_from_payload(payload, params)
                if provider_error is not None:
                    raise provider_error
                status = "success"
                series: object = {}
                for key, value in payload.items():
                    if key.startswith("Time Series ("):
                        series = value
                        break
                record_count = (
                    len(cast(dict[object, object], series))
                    if isinstance(series, dict)
                    else 0
                )
                LOGGER.info(
                    "provider=alpha_vantage symbol=%s timeframe=%s request_status=%s "
                    "records=%d latency_ms=%.1f",
                    symbol,
                    timeframe,
                    status,
                    record_count,
                    latency_ms,
                )
                return payload, latency_ms
            except HTTPError as exc:
                latency_ms = (perf_counter() - started) * 1000
                if exc.code == 429:
                    error: ProviderError = RateLimitError("Alpha Vantage rate limit reached.")
                elif exc.code >= 500:
                    error = ProviderOutageError("Alpha Vantage service returned a server error.")
                else:
                    error = MalformedResponseError(
                        f"Alpha Vantage returned HTTP status {exc.code}."
                    )
                status = "rate_limited" if isinstance(error, RateLimitError) else "http_error"
                self._log_request(symbol, timeframe, status, latency_ms)
                if not self._retry(error, attempt):
                    raise error from None
            except (TimeoutError, socket.timeout, URLError):
                latency_ms = (perf_counter() - started) * 1000
                self._log_request(symbol, timeframe, "network_error", latency_ms)
                error = NetworkError("Alpha Vantage network request failed or timed out.")
                if not self._retry(error, attempt):
                    raise error from None
            except _RETRYABLE_ERRORS as exc:
                latency_ms = (perf_counter() - started) * 1000
                status = "rate_limited" if isinstance(exc, RateLimitError) else "provider_outage"
                self._log_request(symbol, timeframe, status, latency_ms)
                if not self._retry(exc, attempt):
                    raise
            except ProviderError:
                latency_ms = (perf_counter() - started) * 1000
                self._log_request(symbol, timeframe, "invalid_response", latency_ms)
                raise
        raise ProviderOutageError("Alpha Vantage request retries were exhausted.")

    @staticmethod
    def _log_request(symbol: str, timeframe: str, status: str, latency_ms: float) -> None:
        LOGGER.info(
            "provider=alpha_vantage symbol=%s timeframe=%s request_status=%s "
            "records=0 latency_ms=%.1f",
            symbol,
            timeframe,
            status,
            latency_ms,
        )

    def _retry(self, error: ProviderError, attempt: int) -> bool:
        if not isinstance(error, _RETRYABLE_ERRORS) or attempt >= self.max_retries:
            return False
        delay = min(30.0, float(2**attempt))
        LOGGER.warning(
            "provider=alpha_vantage request_status=retry attempt=%d delay_seconds=%.1f error=%s",
            attempt + 1,
            delay,
            type(error).__name__,
        )
        self._sleep(delay)
        return True

    @staticmethod
    def _parse_candle_time(value: str, timeframe: str, market_timezone: str) -> datetime:
        try:
            if timeframe == "daily":
                parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            else:
                local_time = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                parsed = local_time.replace(
                    tzinfo=_timezone_for_market(market_timezone, local_time)
                )
        except ValueError as exc:
            raise MalformedResponseError("Alpha Vantage returned an invalid candle timestamp.") from exc
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _parse_number(value: object, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise MalformedResponseError(f"Alpha Vantage candle has invalid {field}.")
        try:
            number = float(value)
        except ValueError as exc:
            raise MalformedResponseError(
                f"Alpha Vantage candle has invalid {field}."
            ) from exc
        if not math.isfinite(number):
            raise MalformedResponseError(f"Alpha Vantage candle has invalid {field}.")
        return number

    @staticmethod
    def _parse_series(
        payload: dict[str, Any],
        symbol: str,
        timeframe: str,
    ) -> list[MarketData]:
        series_label = _TIME_SERIES_LABELS[timeframe]
        series_value: object = payload.get(series_label)
        if series_value is None:
            if not payload:
                raise EmptyDataError(f"Alpha Vantage returned no candles for {symbol}.")
            if len(payload) <= 1 and any(key in payload for key in ("Meta Data", "Information")):
                raise EmptyDataError(f"Alpha Vantage returned no candles for {symbol}.")
            raise MalformedResponseError("Alpha Vantage response is missing its time-series data.")
        series_mapping = cast(dict[object, object], series_value) if isinstance(series_value, dict) else None
        if (
            series_mapping is None
            or not all(isinstance(key, str) for key in series_mapping.keys())
            or not all(isinstance(value, dict) for value in series_mapping.values())
        ):
            raise MalformedResponseError("Alpha Vantage time-series data has an invalid structure.")
        series = cast(dict[str, dict[str, object]], series_mapping)
        if not series:
            raise EmptyDataError(f"Alpha Vantage returned no candles for {symbol}.")

        metadata_value: object = payload.get("Meta Data", {})
        metadata_mapping = cast(dict[object, object], metadata_value) if isinstance(metadata_value, dict) else None
        if metadata_mapping is None or not all(
            isinstance(key, str) for key in metadata_mapping.keys()
        ):
            raise MalformedResponseError("Alpha Vantage metadata has an invalid structure.")
        metadata = cast(dict[str, object], metadata_mapping)
        market_timezone = str(metadata.get("6. Time Zone", "US/Eastern"))
        records: dict[datetime, MarketData] = {}
        for raw_timestamp, values in series.items():
            timestamp = AlphaVantageProvider._parse_candle_time(
                raw_timestamp, timeframe, market_timezone
            )
            try:
                open_price = AlphaVantageProvider._parse_number(values["1. open"], "open")
                high_price = AlphaVantageProvider._parse_number(values["2. high"], "high")
                low_price = AlphaVantageProvider._parse_number(values["3. low"], "low")
                close_price = AlphaVantageProvider._parse_number(values["4. close"], "close")
                volume = AlphaVantageProvider._parse_number(values.get("5. volume", 0), "volume")
            except KeyError as exc:
                raise MalformedResponseError(
                    "Alpha Vantage candle is missing numeric OHLCV values."
                ) from exc
            try:
                candle = MarketData(
                    timestamp=timestamp,
                    symbol=symbol,
                    exchange=str(metadata.get("Exchange", "")),
                    timeframe=timeframe,
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=volume,
                    source="alpha_vantage",
                )
            except ValueError as exc:
                raise MalformedResponseError(f"Alpha Vantage returned invalid OHLCV data for {symbol}.") from exc
            prior = records.get(timestamp)
            if prior is not None and prior != candle:
                raise MalformedResponseError("Alpha Vantage returned conflicting duplicate candles.")
            records[timestamp] = candle
        if not records:
            raise EmptyDataError(f"Alpha Vantage returned no candles for {symbol}.")
        return sorted(records.values(), key=lambda candle: candle.timestamp)

    @staticmethod
    def _filter_range(
        records: list[MarketData],
        start: datetime | None,
        end: datetime | None,
    ) -> list[MarketData]:
        if start is not None and (start.tzinfo is None or start.utcoffset() is None):
            raise ValueError("start must include a timezone")
        if end is not None and (end.tzinfo is None or end.utcoffset() is None):
            raise ValueError("end must include a timezone")
        start_utc = start.astimezone(timezone.utc) if start else None
        end_utc = end.astimezone(timezone.utc) if end else None
        return [
            record
            for record in records
            if (start_utc is None or record.timestamp >= start_utc)
            and (end_utc is None or record.timestamp <= end_utc)
        ]

    def get_historical_data(
        self,
        symbol: str,
        timeframe: str = "daily",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MarketData]:
        timeframe = timeframe.lower()
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol cannot be empty")
        cache_cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.cache_minutes)
        cached = get_cached_market_data(
            self.database_path,
            symbol,
            timeframe,
            fetched_after=cache_cutoff,
        )
        cache_covers_range = bool(cached)
        if cached and start is not None:
            if start.tzinfo is None or start.utcoffset() is None:
                raise ValueError("start must include a timezone")
            cache_covers_range = cache_covers_range and cached[0].timestamp <= start.astimezone(timezone.utc)
        if cached and end is not None:
            if end.tzinfo is None or end.utcoffset() is None:
                raise ValueError("end must include a timezone")
            cache_covers_range = cache_covers_range and cached[-1].timestamp >= end.astimezone(timezone.utc)
        if cache_covers_range:
            LOGGER.info(
                "provider=alpha_vantage symbol=%s timeframe=%s request_status=cache_hit "
                "records=%d latency_ms=0.0",
                symbol,
                timeframe,
                len(cached),
            )
            return self._filter_range(cached, start, end)

        params = {
            "function": "TIME_SERIES_DAILY" if timeframe == "daily" else "TIME_SERIES_INTRADAY",
            "symbol": symbol,
            "outputsize": "compact",
            "datatype": "json",
        }
        if timeframe != "daily":
            params["interval"] = timeframe
        payload, latency_ms = self._request_json(params, symbol, timeframe)
        records = self._parse_series(payload, symbol, timeframe)
        cache_market_data(self.database_path, records)
        records = self._filter_range(records, start, end)
        LOGGER.info(
            "provider=alpha_vantage symbol=%s timeframe=%s request_status=normalized "
            "records=%d latency_ms=%.1f",
            symbol,
            timeframe,
            len(records),
            latency_ms,
        )
        if not records:
            raise EmptyDataError(f"Alpha Vantage returned no candles for {symbol} in the requested range.")
        return records

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
        symbol = symbol.strip().upper()
        cached = self._metadata_cache.get(symbol)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return dict(cached[1])
        payload, _ = self._request_json(
            {"function": "OVERVIEW", "symbol": symbol},
            symbol,
            "metadata",
        )
        metadata = payload.get("Symbol")
        if not metadata:
            raise EmptyDataError(f"Alpha Vantage returned no metadata for {symbol}.")
        self._metadata_cache[symbol] = (
            now + self.cache_minutes * 60,
            dict(payload),
        )
        return dict(payload)
