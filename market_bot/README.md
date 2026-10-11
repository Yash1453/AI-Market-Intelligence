# Standalone Market Intelligence Bot

This application is intentionally separate from the job agent. Its runtime
configuration, CSV feed, SQLite state, and reports live under `market_bot/`.
The shared provider interface and adapters live under `app/data/`. It has no
dependency on the job-agent package, resume, job database, or its Telegram
settings. Install the dependencies from the repository's `requirements.txt`.
The bot never places live trades.

## Three loops

1. **Market intelligence:** builds a timestamped market snapshot and reports
   each symbol's latest close, available 24-hour change, and feed freshness.
2. **Signal detection:** validates fresh, sufficiently warmed-up prices; alerts
   on configured simple-moving-average crossovers. Telegram is optional, but a
   configured Telegram delivery failure is reported rather than silently ignored.
   Signals are stored before delivery; unsent alerts remain queued and are retried
   on the next loop cycle or process start.
3. **Performance:** records one directional paper prediction per fresh bar,
   settles it after the configured number of bars, and reports hit rate and
   average directional return. Monitoring warns when the configured minimum
   sample has been reached and the hit rate falls below its threshold.

Signals and predictions are analytical examples, not financial advice. Paper
results do not imply future performance.

## Setup

From the repository root, prepare Python and the local configuration:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item market_bot/.env.example market_bot/.env
```

The `.env` file is Git-ignored. The example configuration selects the asset-
aware router: it uses Yahoo Finance chart history for stocks when Twelve Data
is not configured, and Binance public REST for crypto. Add
`TWELVE_DATA_API_KEY` to `market_bot/.env` if using Twelve Data. Configure
`MARKET_TELEGRAM_BOT_TOKEN` and `MARKET_TELEGRAM_CHAT_ID` there to enable signal
alerts. Never commit or share the `.env` file.

For CSV mode, set `DATA_PROVIDER=csv` and place a feed at
`market_bot/data/market_prices.csv`, or set `MARKET_CSV_PATH` to a different
path. CSV supports `timestamp,symbol,close`; optional columns are
`exchange,timeframe,open,high,low,volume`. Missing OHLC values default to the
close and missing volume defaults to zero.

For Alpha Vantage mode, set `DATA_PROVIDER=alpha_vantage` and add the API key
only to `market_bot/.env` as `ALPHA_VANTAGE_API_KEY=...`. Do not place it in
source control or share it in chat. `ALPHA_VANTAGE_BASE_URL` can select a
compatible endpoint. Configure `ASSET_SYMBOLS` and `SCAN_SYMBOLS` as comma-
separated symbols, select `MARKET_TIMEFRAME` from `5min`, `15min`, `30min`,
`60min`, or `daily`, and tune `MAX_SYMBOLS_PER_SCAN`, `API_REQUEST_DELAY`, and
`DATA_CACHE_MINUTES` to bound provider requests. The provider checks its local
SQLite candle cache before requesting data and stores normalized candles there.

For asset-aware routing, set `DATA_PROVIDER=router` and classify scanned assets
in `ASSET_TYPES`, for example:

```dotenv
DATA_PROVIDER=router
ASSET_SYMBOLS=AAPL,EUR/USD,BTC/USDT
SCAN_SYMBOLS=AAPL,EUR/USD,BTC/USDT
ASSET_TYPES=AAPL:stock,EUR/USD:fx,BTC/USDT:crypto
```

The router tries Twelve Data first for each asset, then uses Yahoo Finance chart
history as a stock fallback and Binance public REST klines as a crypto fallback.
Forex has no secondary provider configured. Add `TWELVE_DATA_API_KEY` to
`market_bot/.env` for Twelve Data; it is never logged or sent to Telegram. The
Yahoo Finance fallback is keyless. Binance is REST-only in this adapter; it
does not open a WebSocket. Keep scan symbol counts and request intervals modest.
The one-shot test defaults independently to `ALPHA_VANTAGE_TEST_SYMBOL=AAPL`
and `ALPHA_VANTAGE_TEST_TIMEFRAME=daily`, which can be changed to any supported
symbol and timeframe without changing the scan timeframe.

Check the Alpha Vantage key, fetch one configured symbol, and display normalized
data quality with:

```powershell
python -m app.data.test_provider
```

The test command never displays the API key. Relative paths in the bot's `.env`
are resolved from `market_bot/`.

The CSV must contain a header with `timestamp,symbol,close`. Timestamps must be
ISO-8601 and include a timezone; the bot normalizes them to UTC. Prices must be
finite and positive, with no duplicate symbol/timestamp pairs. Example:

```csv
timestamp,symbol,close
2026-10-04T09:00:00Z,EXAMPLE,101.25
2026-10-04T09:01:00Z,EXAMPLE,101.40
```

Configure `MARKET_FAST_WINDOW` and `MARKET_SLOW_WINDOW` so the fast window is
smaller. `MARKET_PAPER_HOLD_BARS` controls the number of later CSV rows used to
settle each directional prediction. `MARKET_MAX_DATA_AGE_MINUTES` prevents
stale prices from creating new signals or predictions.

To send validated signal alerts, set both `MARKET_TELEGRAM_BOT_TOKEN` and
`MARKET_TELEGRAM_CHAT_ID` in `market_bot/.env`. These settings do not use or
modify the job agent's root `.env`.

## Run

Run one pass of each loop:

```powershell
python -m market_bot report
python -m market_bot signals
python -m market_bot performance
```

Run all loops continuously, or run one continuous loop:

```powershell
python -m market_bot run
python -m market_bot run --loop signals
```

Use `python -m market_bot run --once` to execute all three loops once. Loop
cadences, data age limits, storage paths, and monitoring thresholds are set in
`market_bot/.env.example`. Reports, database state, and market CSV files are
kept in this directory and excluded from Git.

## Managed Windows background run

`market_bot/run_managed.cmd` is used by the optional user-level
`MarketIntelligenceBot` Task Scheduler task. It starts at the current user's
sign-in, uses the asset-aware provider router, writes output to
`market_bot/logs/market_bot.log`, and retries an exited bot after 60 seconds.
The scheduled task runs with the current user's permissions and can be stopped
or disabled by that user or an administrator; it is not a protected or
unstoppable process.

Manage the task from PowerShell:

```powershell
Start-ScheduledTask -TaskName MarketIntelligenceBot
Stop-ScheduledTask -TaskName MarketIntelligenceBot
Disable-ScheduledTask -TaskName MarketIntelligenceBot
```
