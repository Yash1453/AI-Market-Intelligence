"""Read provider credentials from the market bot's private dotenv file."""

from __future__ import annotations

from app.data.adapters.base import ProviderError
from market_bot.config import ROOT


def read_market_secret(name: str, error_message: str) -> str:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        raise ProviderError(error_message)
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if key.strip() != name:
            continue
        if not separator:
            break
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            return value
        break
    raise ProviderError(error_message)


def validate_https_base_url(value: str, setting_name: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"{setting_name} must be an absolute HTTPS URL without query parameters")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
