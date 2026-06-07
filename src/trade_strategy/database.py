from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import pandas as pd


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PACKAGE_ROOT / "data" / "trade_strategy.sqlite3"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tickers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,
    display_symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL CHECK(asset_type IN ('stock', 'crypto')),
    name TEXT,
    currency TEXT,
    exchange TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_downloaded_at TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    ticker_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    adj_close REAL,
    volume REAL,
    PRIMARY KEY (ticker_id, trade_date),
    FOREIGN KEY (ticker_id) REFERENCES tickers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS strategy_configs (
    strategy_name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    params_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS current_prices (
    ticker_id INTEGER PRIMARY KEY,
    price REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticker_id) REFERENCES tickers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS operation_cache (
    ticker_id INTEGER NOT NULL,
    strategy_name TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker_id, strategy_name),
    FOREIGN KEY (ticker_id) REFERENCES tickers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS strategy_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker_id INTEGER NOT NULL,
    strategy_name TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    direction TEXT NOT NULL,
    operation TEXT NOT NULL,
    price REAL NOT NULL,
    signal_price REAL NOT NULL,
    detail TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    signal_class TEXT NOT NULL,
    position_size REAL NOT NULL DEFAULT 0,
    position_notional REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    balance_after REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (ticker_id) REFERENCES tickers(id) ON DELETE CASCADE,
    UNIQUE (ticker_id, strategy_name, sequence)
);

CREATE TABLE IF NOT EXISTS operation_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker_id INTEGER NOT NULL,
    strategy_name TEXT NOT NULL,
    notification_key TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticker_id) REFERENCES tickers(id) ON DELETE CASCADE,
    UNIQUE (ticker_id, strategy_name, notification_key)
);
"""


def database_path() -> Path:
    configured = os.environ.get("TRADE_STRATEGY_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DATABASE_PATH


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    connection = get_connection(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db(path: Path | None = None) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)


def upsert_default_strategy_configs(
    defaults: dict[str, dict[str, Any]], path: Path | None = None
) -> None:
    with connect(path) as connection:
        for strategy_name, params in defaults.items():
            connection.execute(
                """
                INSERT OR IGNORE INTO strategy_configs (strategy_name, params_json)
                VALUES (?, ?)
                """,
                (strategy_name, json.dumps(params)),
            )


def list_tickers(path: Path | None = None) -> list[sqlite3.Row]:
    with connect(path) as connection:
        return list(
            connection.execute(
                """
                SELECT t.*,
                       ph.close AS last_close,
                       ph.trade_date AS last_trade_date
                FROM tickers t
                LEFT JOIN price_history ph
                  ON ph.ticker_id = t.id
                 AND ph.trade_date = (
                    SELECT MAX(trade_date)
                    FROM price_history
                    WHERE ticker_id = t.id
                 )
                ORDER BY t.asset_type, t.display_symbol
                """
            )
        )


def get_ticker(ticker_id: int, path: Path | None = None) -> sqlite3.Row | None:
    with connect(path) as connection:
        return connection.execute(
            "SELECT * FROM tickers WHERE id = ?", (ticker_id,)
        ).fetchone()


def add_ticker(
    symbol: str,
    display_symbol: str,
    asset_type: str,
    name: str | None = None,
    currency: str | None = None,
    exchange: str | None = None,
    path: Path | None = None,
) -> int:
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO tickers (symbol, display_symbol, asset_type, name, currency, exchange)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                display_symbol = excluded.display_symbol,
                asset_type = excluded.asset_type,
                name = COALESCE(excluded.name, tickers.name),
                currency = COALESCE(excluded.currency, tickers.currency),
                exchange = COALESCE(excluded.exchange, tickers.exchange)
            """,
            (symbol, display_symbol, asset_type, name, currency, exchange),
        )
        row = connection.execute(
            "SELECT id FROM tickers WHERE symbol = ?", (symbol,)
        ).fetchone()
        return int(row["id"])


def update_ticker_metadata(
    ticker_id: int,
    name: str | None,
    currency: str | None,
    exchange: str | None,
    path: Path | None = None,
) -> None:
    with connect(path) as connection:
        connection.execute(
            """
            UPDATE tickers
               SET name = COALESCE(?, name),
                   currency = COALESCE(?, currency),
                   exchange = COALESCE(?, exchange)
             WHERE id = ?
            """,
            (name, currency, exchange, ticker_id),
        )


def delete_ticker(ticker_id: int, path: Path | None = None) -> None:
    with connect(path) as connection:
        connection.execute("DELETE FROM tickers WHERE id = ?", (ticker_id,))


def save_history(
    ticker_id: int, history: pd.DataFrame, path: Path | None = None
) -> int:
    if history.empty:
        return 0

    rows = []
    for trade_date, row in history.iterrows():
        rows.append(
            (
                ticker_id,
                trade_date.date().isoformat(),
                _optional_float(row.get("Open")),
                _optional_float(row.get("High")),
                _optional_float(row.get("Low")),
                _required_float(row.get("Close")),
                _optional_float(row.get("Adj Close")),
                _optional_float(row.get("Volume")),
            )
        )

    with connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO price_history
                (ticker_id, trade_date, open, high, low, close, adj_close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker_id, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                adj_close = excluded.adj_close,
                volume = excluded.volume
            """,
            rows,
        )
        connection.execute(
            "UPDATE tickers SET last_downloaded_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ticker_id,),
        )
    return len(rows)


def load_history(ticker_id: int, path: Path | None = None) -> pd.DataFrame:
    with connect(path) as connection:
        rows = connection.execute(
            """
            SELECT trade_date, open, high, low, close, adj_close, volume
            FROM price_history
            WHERE ticker_id = ?
            ORDER BY trade_date
            """,
            (ticker_id,),
        ).fetchall()

    if not rows:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "adj_close", "volume"]
        )

    frame = pd.DataFrame([dict(row) for row in rows])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.set_index("trade_date")
    return frame


def list_strategy_configs(path: Path | None = None) -> dict[str, dict[str, Any]]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT strategy_name, enabled, params_json FROM strategy_configs"
        ).fetchall()

    configs = {}
    for row in rows:
        configs[row["strategy_name"]] = {
            "enabled": bool(row["enabled"]),
            "params": json.loads(row["params_json"]),
        }
    return configs


def update_strategy_config(
    strategy_name: str,
    enabled: bool,
    params: dict[str, Any],
    path: Path | None = None,
) -> None:
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO strategy_configs (strategy_name, enabled, params_json, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(strategy_name) DO UPDATE SET
                enabled = excluded.enabled,
                params_json = excluded.params_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (strategy_name, int(enabled), json.dumps(params)),
        )


def save_current_price(
    ticker_id: int,
    price: float,
    path: Path | None = None,
) -> None:
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO current_prices (ticker_id, price, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(ticker_id) DO UPDATE SET
                price = excluded.price,
                updated_at = CURRENT_TIMESTAMP
            """,
            (ticker_id, price),
        )


def save_realtime_candle(
    ticker_id: int,
    trade_date: date,
    price: float,
    path: Path | None = None,
) -> None:
    trade_date_text = trade_date.isoformat()
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO price_history
                (ticker_id, trade_date, open, high, low, close, adj_close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(ticker_id, trade_date) DO UPDATE SET
                high = MAX(COALESCE(price_history.high, excluded.high), excluded.close),
                low = MIN(COALESCE(price_history.low, excluded.low), excluded.close),
                close = excluded.close,
                adj_close = excluded.adj_close
            """,
            (
                ticker_id,
                trade_date_text,
                price,
                price,
                price,
                price,
                price,
            ),
        )


def list_current_prices(path: Path | None = None) -> dict[int, sqlite3.Row]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT ticker_id, price, updated_at FROM current_prices"
        ).fetchall()
    return {int(row["ticker_id"]): row for row in rows}


def get_operation_cache_key(
    ticker_id: int,
    strategy_name: str,
    path: Path | None = None,
) -> str | None:
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT cache_key
            FROM operation_cache
            WHERE ticker_id = ? AND strategy_name = ?
            """,
            (ticker_id, strategy_name),
        ).fetchone()
    return row["cache_key"] if row is not None else None


def save_strategy_operations(
    ticker_id: int,
    strategy_name: str,
    operations: list[Any],
    cache_key: str,
    path: Path | None = None,
) -> None:
    rows = [
        (
            ticker_id,
            strategy_name,
            index,
            operation.trade_date,
            operation.direction,
            operation.operation,
            operation.price,
            operation.signal_price,
            operation.detail,
            json.dumps(operation.metrics),
            operation.signal_class,
            operation.position_size,
            operation.position_notional,
            operation.realized_pnl,
            operation.balance_after,
        )
        for index, operation in enumerate(operations)
    ]

    with connect(path) as connection:
        connection.execute(
            """
            DELETE FROM strategy_operations
            WHERE ticker_id = ? AND strategy_name = ?
            """,
            (ticker_id, strategy_name),
        )
        if rows:
            connection.executemany(
                """
                INSERT INTO strategy_operations
                    (
                        ticker_id, strategy_name, sequence, trade_date, direction,
                        operation, price, signal_price, detail, metrics_json,
                        signal_class, position_size, position_notional,
                        realized_pnl, balance_after
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        connection.execute(
            """
            INSERT INTO operation_cache (ticker_id, strategy_name, cache_key, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(ticker_id, strategy_name) DO UPDATE SET
                cache_key = excluded.cache_key,
                updated_at = CURRENT_TIMESTAMP
            """,
            (ticker_id, strategy_name, cache_key),
        )


def load_strategy_operations(
    ticker_id: int,
    strategy_name: str,
    path: Path | None = None,
) -> list[sqlite3.Row]:
    with connect(path) as connection:
        return list(
            connection.execute(
                """
                SELECT trade_date, direction, operation, price, signal_price,
                       detail, metrics_json, signal_class, position_size,
                       position_notional, realized_pnl, balance_after
                FROM strategy_operations
                WHERE ticker_id = ? AND strategy_name = ?
                ORDER BY sequence
                """,
                (ticker_id, strategy_name),
            )
        )


def mark_operation_notification_sent(
    ticker_id: int,
    strategy_name: str,
    notification_key: str,
    path: Path | None = None,
) -> bool:
    with connect(path) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO operation_notifications
                (ticker_id, strategy_name, notification_key)
            VALUES (?, ?, ?)
            """,
            (ticker_id, strategy_name, notification_key),
        )
        return cursor.rowcount == 1


def operation_notification_sent(
    ticker_id: int,
    strategy_name: str,
    notification_key: str,
    path: Path | None = None,
) -> bool:
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM operation_notifications
            WHERE ticker_id = ? AND strategy_name = ? AND notification_key = ?
            """,
            (ticker_id, strategy_name, notification_key),
        ).fetchone()
    return row is not None


def _optional_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _required_float(value: Any) -> float:
    if value is None or pd.isna(value):
        raise ValueError("Close price is required for every history row.")
    return float(value)
