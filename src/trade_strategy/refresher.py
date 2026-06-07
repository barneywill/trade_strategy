from __future__ import annotations

import logging
import os
import threading
from datetime import date
from pathlib import Path

import pandas as pd

from . import database
from .common_settings import COMMON_DEFAULTS, common_params
from .market_calendar import (
    current_realtime_data_date,
    is_us_stock_market_open,
    latest_completed_data_date,
    seconds_until_next_utc_time,
)
from .market_data import download_history, fetch_current_price, fetch_current_prices
from .notifications import (
    operation_notification_key,
    send_operation_notification,
    telegram_config,
)
from .operation_manager import OperationManager
from .strategies import STRATEGIES


LOGGER = logging.getLogger(__name__)
_SCHEDULER_STARTED = False
_REALTIME_UPDATER_STARTED = False


def start_history_refresher(
    db_path: Path,
    full_period: str,
    daily_period: str = "1mo",
    full_start: str | None = None,
) -> None:
    global _SCHEDULER_STARTED

    if _SCHEDULER_STARTED:
        return
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return

    _SCHEDULER_STARTED = True
    thread = threading.Thread(
        target=_run_scheduler,
        args=(db_path, full_period, daily_period, full_start),
        name="trade-strategy-history-refresher",
        daemon=True,
    )
    thread.start()


def start_realtime_price_refresher(
    db_path: Path,
    frequency_seconds: int,
) -> None:
    global _REALTIME_UPDATER_STARTED

    if _REALTIME_UPDATER_STARTED:
        return
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return

    _REALTIME_UPDATER_STARTED = True
    thread = threading.Thread(
        target=_run_realtime_price_scheduler,
        args=(db_path, max(30, int(frequency_seconds))),
        name="trade-strategy-realtime-price-refresher",
        daemon=True,
    )
    thread.start()


def backfill_all_tickers(
    db_path: Path,
    period: str,
    start: str | None = None,
) -> dict[str, int]:
    results: dict[str, int] = {}
    for ticker in database.list_tickers(db_path):
        results[ticker["symbol"]] = refresh_ticker_if_needed(
            ticker,
            db_path,
            period,
            force=True,
            start=start,
        )
    return results


def refresh_due_tickers(
    db_path: Path,
    period: str,
    force: bool = False,
) -> dict[str, int]:
    results: dict[str, int] = {}
    for ticker in database.list_tickers(db_path):
        results[ticker["symbol"]] = refresh_ticker_if_needed(
            ticker,
            db_path,
            period,
            force=force,
        )
    return results


def refresh_ticker_if_needed(
    ticker,
    db_path: Path,
    period: str,
    force: bool = False,
    start: str | None = None,
) -> int:
    expected_date = latest_completed_data_date(ticker["asset_type"])
    last_trade_date = None if force else _parse_date(_row_get(ticker, "last_trade_date"))

    if not force and last_trade_date is not None and last_trade_date >= expected_date:
        return 0

    history = download_history(ticker["symbol"], period, start=start)
    history = _keep_completed_rows(history, expected_date)
    saved_rows = database.save_history(ticker["id"], history, db_path)
    if saved_rows:
        OperationManager(db_path).refresh_ticker(int(ticker["id"]))
    return saved_rows


def _run_scheduler(
    db_path: Path,
    full_period: str,
    daily_period: str,
    full_start: str | None,
) -> None:
    _run_job(
        "startup missing history refresh",
        refresh_due_tickers,
        db_path,
        daily_period,
    )

    while True:
        params = common_params(database.list_strategy_configs(db_path))
        hour, minute = _parse_daily_data_fetch_time(
            params.get(
                "daily_data_fetch_time",
                COMMON_DEFAULTS["daily_data_fetch_time"],
            )
        )
        sleep_seconds = seconds_until_next_utc_time(hour, minute)
        threading.Event().wait(sleep_seconds)
        _run_job("daily history refresh", refresh_due_tickers, db_path, daily_period, True)


def refresh_realtime_prices(db_path: Path) -> dict[str, float | None]:
    results: dict[str, float | None] = {}
    stock_market_open = is_us_stock_market_open()
    tickers = database.list_tickers(db_path)
    eligible_tickers = []
    configs = database.list_strategy_configs(db_path)
    operation_manager = OperationManager(db_path)

    for ticker in tickers:
        if ticker["asset_type"] == "stock" and not stock_market_open:
            results[ticker["symbol"]] = None
            continue
        eligible_tickers.append(ticker)

    batch_prices = fetch_current_prices([ticker["symbol"] for ticker in eligible_tickers])

    for ticker in eligible_tickers:
        price = batch_prices.get(ticker["symbol"])
        if price is None:
            price = fetch_current_price(ticker["symbol"])
        results[ticker["symbol"]] = price
        realtime_date = current_realtime_data_date(ticker["asset_type"])
        if price is not None:
            database.save_current_price(ticker["id"], price, db_path)
            database.save_realtime_candle(
                ticker["id"],
                realtime_date,
                price,
                db_path,
            )
        previous_operation_keys = _operation_notification_keys_for_ticker(ticker, db_path)
        if price is None or not operation_manager.realtime_operation_triggered(
            int(ticker["id"]),
            price,
            configs,
        ):
            continue

        history = download_history(ticker["symbol"], "5d")
        if not history.empty:
            database.save_history(ticker["id"], history, db_path)
            database.save_realtime_candle(ticker["id"], realtime_date, price, db_path)
            _refresh_ticker_operations_after_realtime_change(
                ticker,
                db_path,
                previous_operation_keys,
            )

    return results


def _run_realtime_price_scheduler(db_path: Path, frequency_seconds: int) -> None:
    while True:
        params = common_params(database.list_strategy_configs(db_path))
        frequency_seconds = max(
            30,
            int(params.get("realtime_update_frequency", COMMON_DEFAULTS["realtime_update_frequency"])),
        )
        if params.get("enable_realtime_updates", False):
            _run_job("realtime price refresh", refresh_realtime_prices, db_path)
        threading.Event().wait(frequency_seconds)


def _refresh_ticker_operations_after_realtime_change(
    ticker,
    db_path: Path,
    previous_operation_keys: dict[str, set[str]] | None = None,
) -> None:
    configs = database.list_strategy_configs(db_path)
    common = common_params(configs)
    notification_config = telegram_config(common)
    history = database.load_history(ticker["id"], db_path)
    latest_date = _latest_history_date(history)
    manager = OperationManager(db_path)

    for strategy_name, strategy in STRATEGIES.items():
        config = configs.get(
            strategy_name,
            {"enabled": True, "params": strategy.default_params},
        )
        if not config["enabled"]:
            continue

        previous_keys = (
            previous_operation_keys.get(strategy_name, set())
            if previous_operation_keys is not None
            else {
                _operation_row_notification_key(row)
                for row in database.load_strategy_operations(
                    int(ticker["id"]),
                    strategy_name,
                    db_path,
                )
            }
        )
        operations = manager.operations_for(
            int(ticker["id"]),
            strategy_name,
            strategy,
            history,
            config["params"],
        )
        if not notification_config.configured or latest_date is None:
            continue

        for operation in operations:
            notification_key = operation_notification_key(operation)
            if operation.trade_date != latest_date or notification_key in previous_keys:
                continue
            if database.operation_notification_sent(
                int(ticker["id"]),
                strategy_name,
                notification_key,
                db_path,
            ):
                continue
            if send_operation_notification(
                notification_config,
                ticker,
                strategy.label,
                operation,
            ):
                database.mark_operation_notification_sent(
                    int(ticker["id"]),
                    strategy_name,
                    notification_key,
                    db_path,
                )


def _latest_history_date(history: pd.DataFrame) -> str | None:
    frame = history.dropna(subset=["close"])
    if frame.empty:
        return None
    return frame.index[-1].date().isoformat()


def _operation_row_notification_key(row) -> str:
    return "|".join(
        [
            row["trade_date"],
            row["direction"],
            row["operation"],
            f"{float(row['signal_price']):.8f}",
        ]
    )


def _operation_notification_keys_for_ticker(ticker, db_path: Path) -> dict[str, set[str]]:
    return {
        strategy_name: {
            _operation_row_notification_key(row)
            for row in database.load_strategy_operations(
                int(ticker["id"]),
                strategy_name,
                db_path,
            )
        }
        for strategy_name in STRATEGIES
    }


def _run_job(label: str, function, *args) -> None:
    try:
        results = function(*args)
        updated = sum(1 for row_count in results.values() if row_count)
        LOGGER.info("%s completed for %s ticker(s).", label, updated)
    except Exception:
        LOGGER.exception("%s failed.", label)


def _keep_completed_rows(history: pd.DataFrame, through_date: date) -> pd.DataFrame:
    if history.empty:
        return history
    return history[history.index.date <= through_date]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _row_get(row, key: str):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _parse_daily_data_fetch_time(value) -> tuple[int, int]:
    try:
        hour_text, minute_text = str(value).strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return 0, 1

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return 0, 1
    return hour, minute
