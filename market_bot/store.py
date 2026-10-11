"""Persistence owned exclusively by the market-intelligence bot."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.data.adapters.base import MarketData
from market_bot.signals import Signal


@dataclass(frozen=True)
class Prediction:
    symbol: str
    timestamp: datetime
    entry_price: float
    direction: int


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals (
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            direction TEXT NOT NULL,
            close REAL NOT NULL,
            fast_average REAL NOT NULL,
            slow_average REAL NOT NULL,
            telegram_sent INTEGER NOT NULL DEFAULT 0 CHECK (telegram_sent IN (0, 1)),
            PRIMARY KEY (symbol, timestamp)
        );
        CREATE TABLE IF NOT EXISTS predictions (
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            entry_price REAL NOT NULL,
            direction INTEGER NOT NULL CHECK (direction IN (-1, 1)),
            exit_price REAL,
            return_pct REAL,
            hit INTEGER CHECK (hit IN (0, 1)),
            PRIMARY KEY (symbol, timestamp)
        );
        CREATE TABLE IF NOT EXISTS market_data (
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            exchange TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, timeframe, timestamp)
        );
        CREATE TABLE IF NOT EXISTS market_data_cache (
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, timeframe)
        );
        """
    )
    return connection


@contextmanager
def _connection(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(_connect(path)) as connection:
        with connection:
            yield connection


def record_signal(path: Path, signal: Signal) -> bool:
    with _connection(path) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO signals
                (symbol, timestamp, direction, close, fast_average, slow_average)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                signal.symbol,
                signal.timestamp.isoformat(),
                signal.direction,
                signal.close,
                signal.fast_average,
                signal.slow_average,
            ),
        )
        return cursor.rowcount == 1


def pending_signal_alerts(path: Path) -> list[Signal]:
    with _connection(path) as connection:
        rows = connection.execute(
            """
            SELECT symbol, timestamp, direction, close, fast_average, slow_average
            FROM signals
            WHERE telegram_sent = 0
            ORDER BY timestamp
            """
        ).fetchall()
    return [
        Signal(
            symbol=row["symbol"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            direction=row["direction"],
            close=float(row["close"]),
            fast_average=float(row["fast_average"]),
            slow_average=float(row["slow_average"]),
        )
        for row in rows
    ]


def mark_signal_sent(path: Path, signal: Signal) -> None:
    with _connection(path) as connection:
        connection.execute(
            """
            UPDATE signals SET telegram_sent = 1
            WHERE symbol = ? AND timestamp = ?
            """,
            (signal.symbol, signal.timestamp.isoformat()),
        )


def record_prediction(path: Path, bar: MarketData, direction: int) -> bool:
    if direction not in {-1, 1}:
        raise ValueError("Prediction direction must be -1 or 1")
    with _connection(path) as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO predictions (symbol, timestamp, entry_price, direction) VALUES (?, ?, ?, ?)",
            (bar.symbol, bar.timestamp.isoformat(), bar.close, direction),
        )
        return cursor.rowcount == 1


def pending_predictions(path: Path) -> list[Prediction]:
    with _connection(path) as connection:
        rows = connection.execute(
            """
            SELECT symbol, timestamp, entry_price, direction
            FROM predictions
            WHERE exit_price IS NULL
            """
        ).fetchall()
    return [
        Prediction(
            symbol=row["symbol"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            entry_price=float(row["entry_price"]),
            direction=int(row["direction"]),
        )
        for row in rows
    ]


def latest_prediction_timestamps(path: Path) -> dict[str, datetime]:
    with _connection(path) as connection:
        rows = connection.execute(
            """
            SELECT symbol, MAX(timestamp) AS timestamp
            FROM predictions
            GROUP BY symbol
            """
        ).fetchall()
    return {
        row["symbol"]: datetime.fromisoformat(row["timestamp"])
        for row in rows
    }


def settle_prediction(path: Path, prediction: Prediction, exit_price: float) -> float:
    if exit_price <= 0:
        raise ValueError("exit_price must be positive")
    trade_return = (exit_price - prediction.entry_price) / prediction.entry_price
    directed_return = trade_return * prediction.direction
    with _connection(path) as connection:
        connection.execute(
            """
            UPDATE predictions
            SET exit_price = ?, return_pct = ?, hit = ?
            WHERE symbol = ? AND timestamp = ? AND exit_price IS NULL
            """,
            (
                exit_price,
                directed_return,
                int(directed_return > 0),
                prediction.symbol,
                prediction.timestamp.isoformat(),
            ),
        )
    return directed_return


def prediction_metrics(path: Path) -> tuple[int, int, float | None, float | None]:
    with _connection(path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(hit), 0) AS hits,
                   AVG(hit) AS hit_rate,
                   AVG(return_pct) AS average_return
            FROM predictions
            WHERE exit_price IS NOT NULL
            """
        ).fetchone()
    count = int(row["count"])
    return (
        count,
        int(row["hits"]),
        float(row["hit_rate"]) if row["hit_rate"] is not None else None,
        float(row["average_return"]) if row["average_return"] is not None else None,
    )


def cache_market_data(path: Path, records: list[MarketData]) -> None:
    if not records:
        return
    fetched_at = datetime.now(timezone.utc).isoformat()
    with _connection(path) as connection:
        connection.executemany(
            """
            INSERT INTO market_data
                (symbol, timeframe, timestamp, exchange, open, high, low, close, volume, source, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, timestamp) DO UPDATE SET
                exchange = excluded.exchange,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                source = excluded.source,
                fetched_at = excluded.fetched_at
            """,
            [
                (
                    record.symbol,
                    record.timeframe,
                    record.timestamp.astimezone(timezone.utc).isoformat(),
                    record.exchange,
                    record.open,
                    record.high,
                    record.low,
                    record.close,
                    record.volume,
                    record.source,
                    fetched_at,
                )
                for record in records
            ],
        )
        for symbol, timeframe in {(record.symbol, record.timeframe) for record in records}:
            connection.execute(
                """
                INSERT INTO market_data_cache (symbol, timeframe, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(symbol, timeframe) DO UPDATE SET fetched_at = excluded.fetched_at
                """,
                (symbol, timeframe, fetched_at),
            )


def get_cached_market_data(
    path: Path,
    symbol: str,
    timeframe: str,
    fetched_after: datetime,
) -> list[MarketData]:
    if not path.is_file():
        return []
    with _connection(path) as connection:
        cache_row = connection.execute(
            """
            SELECT fetched_at FROM market_data_cache
            WHERE symbol = ? AND timeframe = ?
            """,
            (symbol, timeframe),
        ).fetchone()
        if cache_row is None:
            return []
        fetched_at = datetime.fromisoformat(cache_row["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.astimezone()
        if fetched_at < fetched_after.astimezone(fetched_at.tzinfo):
            return []
        rows = connection.execute(
            """
            SELECT timestamp, symbol, exchange, timeframe, open, high, low, close, volume, source
            FROM market_data
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp
            """,
            (symbol, timeframe),
        ).fetchall()
    return [
        MarketData(
            timestamp=datetime.fromisoformat(row["timestamp"]),
            symbol=row["symbol"],
            exchange=row["exchange"],
            timeframe=row["timeframe"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            source=row["source"],
        )
        for row in rows
    ]
