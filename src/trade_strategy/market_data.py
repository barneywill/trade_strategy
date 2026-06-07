from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar

import pandas as pd


FETCH_RETRY_ATTEMPTS = 5
FETCH_RETRY_DELAY_SECONDS = 5.0
T = TypeVar("T")


@dataclass(frozen=True)
class TickerMetadata:
    name: str | None = None
    currency: str | None = None
    exchange: str | None = None


def normalize_symbol(raw_symbol: str, asset_type: str) -> tuple[str, str]:
    symbol = raw_symbol.strip().upper()
    if not symbol:
        raise ValueError("Ticker symbol is required.")

    if asset_type == "crypto":
        if "-" not in symbol:
            symbol = f"{symbol}-USD"
        display_symbol = symbol.replace("-USD", "")
        return symbol, display_symbol

    return symbol, symbol


def download_history(
    symbol: str,
    period: str | None = "2y",
    start: str | None = None,
) -> pd.DataFrame:
    import yfinance as yf

    download_args = {
        "interval": "1d",
        "auto_adjust": False,
        "repair": True,
        "progress": False,
        "threads": False,
    }
    if start is not None:
        download_args["start"] = start
    elif period is not None:
        download_args["period"] = period

    try:
        history = _retry_fetch(
            lambda: yf.download(symbol, **download_args),
            alarm_context=f"Download history for {symbol}",
        )
    except ModuleNotFoundError:
        # yfinance repair mode can require scipy; fallback keeps downloads working.
        download_args["repair"] = False
        history = _retry_fetch(
            lambda: yf.download(symbol, **download_args),
            alarm_context=f"Download history for {symbol}",
        )
    if history.empty and download_args.get("repair"):
        download_args["repair"] = False
        history = _retry_fetch(
            lambda: yf.download(symbol, **download_args),
            alarm_context=f"Download history for {symbol}",
        )
    if history.empty:
        return history

    if isinstance(history.columns, pd.MultiIndex):
        history.columns = _flatten_history_columns(history.columns)
    history = history.loc[:, ~history.columns.duplicated()]

    return history.dropna(subset=["Close"])


def fetch_metadata(symbol: str) -> TickerMetadata:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    info = {}
    try:
        info = _retry_fetch(
            ticker.get_info,
            alarm_context=f"Fetch metadata for {symbol}",
        )
    except Exception:
        info = {}

    return TickerMetadata(
        name=info.get("shortName") or info.get("longName"),
        currency=info.get("currency"),
        exchange=info.get("exchange") or info.get("fullExchangeName"),
    )


def fetch_current_price(symbol: str) -> float | None:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    try:
        value = _retry_fetch(lambda: ticker.fast_info.get("last_price"))
        if value is not None:
            return float(value)
    except Exception:
        pass

    try:
        history = _retry_fetch(
            lambda: ticker.history(period="1d", interval="1m"),
            alarm_context=f"Fetch current price for {symbol}",
        )
        if not history.empty:
            return float(history["Close"].dropna().iloc[-1])
    except Exception:
        pass

    return None


def fetch_current_prices(symbols: list[str]) -> dict[str, float | None]:
    if not symbols:
        return {}

    import yfinance as yf

    unique_symbols = list(dict.fromkeys(symbols))
    try:
        history = _retry_fetch(
            lambda: yf.download(
                unique_symbols,
                period="1d",
                interval="1m",
                auto_adjust=False,
                progress=False,
                threads=True,
                group_by="ticker",
            ),
            alarm_context=f"Fetch current prices for {', '.join(unique_symbols)}",
        )
    except Exception:
        return {symbol: None for symbol in unique_symbols}

    return {
        symbol: _latest_close_for_symbol(history, symbol)
        for symbol in unique_symbols
    }


def _retry_fetch(
    function: Callable[[], T],
    attempts: int = FETCH_RETRY_ATTEMPTS,
    delay_seconds: float = FETCH_RETRY_DELAY_SECONDS,
    alarm_context: str | None = None,
) -> T:
    last_error = None
    for attempt in range(max(1, attempts)):
        try:
            return function()
        except ModuleNotFoundError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1 and delay_seconds > 0:
                time.sleep(delay_seconds)

    if last_error is not None:
        if alarm_context is not None:
            _send_fetch_failure_alarm(alarm_context, last_error, max(1, attempts))
        raise last_error
    raise RuntimeError("Fetch retry failed without an exception.")


def _send_fetch_failure_alarm(context: str, error: Exception, attempts: int) -> bool:
    try:
        from . import database
        from .common_settings import common_params
        from .notifications import send_alarm_notification, telegram_config

        params = common_params(database.list_strategy_configs(database.database_path()))
        config = telegram_config(params)
        message = "\n".join(
            [
                "Trade Strategy Alarm",
                f"Task: {context}",
                f"Status: failed after {attempts} attempts",
                f"Error: {type(error).__name__}: {error}",
            ]
        )
        return send_alarm_notification(config, message)
    except Exception:
        return False


def _latest_close_for_symbol(history: pd.DataFrame, symbol: str) -> float | None:
    if history.empty:
        return None

    close = None
    if isinstance(history.columns, pd.MultiIndex):
        close = _multi_index_close(history, symbol)
    elif "Close" in history.columns:
        close = history["Close"]

    if close is None:
        return None

    close = close.dropna()
    if close.empty:
        return None
    return float(close.iloc[-1])


def _multi_index_close(history: pd.DataFrame, symbol: str):
    columns = history.columns
    for level in range(columns.nlevels):
        values = columns.get_level_values(level)
        if symbol in set(values):
            try:
                symbol_frame = history.xs(symbol, axis=1, level=level)
            except KeyError:
                continue
            if "Close" in symbol_frame.columns:
                close = symbol_frame["Close"]
                return close.iloc[:, 0] if isinstance(close, pd.DataFrame) else close

    for level in range(columns.nlevels):
        values = columns.get_level_values(level)
        if "Close" in set(values):
            try:
                close_frame = history.xs("Close", axis=1, level=level)
            except KeyError:
                continue
            if isinstance(close_frame, pd.Series):
                return close_frame
            if symbol in close_frame.columns:
                close = close_frame[symbol]
                return close.iloc[:, 0] if isinstance(close, pd.DataFrame) else close

    return None


def _flatten_history_columns(columns: pd.MultiIndex) -> pd.Index:
    for level in range(columns.nlevels):
        values = columns.get_level_values(level)
        if {"Open", "High", "Low", "Close"}.issubset(set(values)):
            return pd.Index(values)
    return pd.Index(columns.get_level_values(0))
