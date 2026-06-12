from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, flash, redirect, render_template, request, session, url_for

from . import database
from .backtest import yearly_backtest
from .charting import build_operation_chart
from .common_settings import (
    COMMON_CONFIG_NAME,
    COMMON_DEFAULTS,
    COMMON_PARAMETER_SPECS,
    common_params,
)
from .market_data import (
    download_history,
    fetch_metadata,
    normalize_symbol,
)
from .market_calendar import (
    latest_completed_data_date,
    is_us_stock_market_open,
)
from .logging_config import DEFAULT_LOG_DIR, configure_file_logging
from .operation_manager import OperationManager
from .refresher import (
    refresh_ticker_if_needed,
    start_history_refresher,
    start_realtime_price_refresher,
)
from .strategies import (
    EXIT,
    STRATEGIES,
    TradeStrategy,
    default_strategy_params,
)


LOGGER = logging.getLogger(__name__)


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get(
            "TRADE_STRATEGY_SECRET_KEY",
            os.environ.get("TRADE_STRATEGY_ACCESS_PASSWORD") or "dev",
        ),
        DATABASE=database.database_path(),
        HISTORY_PERIOD="2y",
        HISTORY_START_DATE="2000-01-01",
        DAILY_HISTORY_PERIOD="1mo",
        AUTO_REFRESH_ENABLED=os.environ.get("TRADE_STRATEGY_AUTO_REFRESH", "1") != "0",
        ACCESS_PASSWORD=os.environ.get("TRADE_STRATEGY_ACCESS_PASSWORD", ""),
        LOG_DIR=os.environ.get("TRADE_STRATEGY_LOG_DIR", str(DEFAULT_LOG_DIR)),
    )
    if test_config:
        app.config.update(test_config)

    log_file = configure_file_logging(app.config["LOG_DIR"])
    LOGGER.info("Trade Strategy app starting; log_file=%s", log_file)

    db_path = Path(app.config["DATABASE"])
    database.init_db(db_path)
    database.upsert_default_strategy_configs(default_strategy_params(), db_path)
    database.upsert_default_strategy_configs(
        {COMMON_CONFIG_NAME: COMMON_DEFAULTS},
        db_path,
    )
    operation_manager = OperationManager(db_path)
    saved_common_params = common_params(database.list_strategy_configs(db_path))
    if app.config["AUTO_REFRESH_ENABLED"] and not app.config.get("TESTING"):
        start_history_refresher(
            db_path,
            app.config["HISTORY_PERIOD"],
            app.config["DAILY_HISTORY_PERIOD"],
            app.config["HISTORY_START_DATE"],
        )
        start_realtime_price_refresher(
            db_path,
            int(saved_common_params.get("realtime_update_frequency", 300)),
        )

    @app.context_processor
    def auth_template_context():
        return {
            "access_password_enabled": access_password_enabled(app),
            "access_granted": bool(session.get("access_granted")),
            "page_style": page_style(database.list_strategy_configs(db_path)),
        }

    @app.before_request
    def require_access_password():
        if not access_password_enabled(app):
            return None
        if session.get("access_granted"):
            return None
        if request.endpoint in {"login", "static"}:
            return None
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not access_password_enabled(app):
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            password = request.form.get("password", "")
            if hmac.compare_digest(password, str(app.config["ACCESS_PASSWORD"])):
                session["access_granted"] = True
                next_url = request.form.get("next") or url_for("dashboard")
                if not is_local_next_url(next_url):
                    next_url = url_for("dashboard")
                return redirect(next_url)
            flash("Incorrect password.", "error")

        return render_template(
            "login.html",
            next_url=request.args.get("next", url_for("dashboard")),
        )

    @app.post("/logout")
    def logout():
        session.pop("access_granted", None)
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        stock_market_open = is_us_stock_market_open()
        tickers = database.list_tickers(db_path)
        configs = database.list_strategy_configs(db_path)
        saved_common_params = common_params(configs)
        timezone_offset = parse_timezone_offset(
            saved_common_params.get("timezone_offset", "+0")
        )
        realtime_prices = database.list_current_prices(db_path)
        recent_closes = database.list_recent_closes(db_path)
        cached_operations = database.list_latest_strategy_operations(db_path)
        rows = []

        for ticker in tickers:
            ticker_id = int(ticker["id"])
            realtime_price = realtime_prices.get(int(ticker["id"]))
            use_realtime_price = (
                saved_common_params.get("enable_realtime_updates", False)
                and realtime_price is not None
                and (ticker["asset_type"] != "stock" or stock_market_open)
            )
            compare_to_latest_close = use_realtime_price
            current_price = (
                realtime_price["price"]
                if use_realtime_price
                else ticker["last_close"]
            )
            daily_change_pct = calculate_daily_change_pct(
                current_price,
                recent_closes.get(ticker_id, []),
                ticker["asset_type"],
                compare_to_latest_close,
            )
            strategy_signals = evaluate_strategies(
                cached_operations.get(ticker_id, {}),
                latest_strategy_dates(ticker, recent_closes.get(ticker_id, [])),
                configs,
            )
            rows.append(
                {
                    "ticker": ticker,
                    "current_price": current_price,
                    "daily_change_pct": daily_change_pct,
                    "current_price_updated_at": format_dashboard_timestamp(
                        realtime_price["updated_at"] if use_realtime_price else None,
                        timezone_offset,
                    ),
                    "last_downloaded_at": format_dashboard_timestamp(
                        ticker["last_downloaded_at"],
                        timezone_offset,
                    ),
                    "strategy_signals": strategy_signals,
                }
            )

        return render_template(
            "dashboard.html",
            rows=rows,
            row_groups=group_dashboard_rows(
                rows,
                saved_common_params.get("default_group_symbols", ""),
            ),
            strategies=STRATEGIES,
            common_params=saved_common_params,
        )

    @app.post("/tickers")
    def add_ticker():
        raw_symbol = request.form.get("symbol", "")
        asset_type = request.form.get("asset_type", "stock")

        if asset_type not in {"stock", "crypto"}:
            flash("Choose either US stock or crypto.", "error")
            return redirect(url_for("dashboard"))

        try:
            symbol, display_symbol = normalize_symbol(raw_symbol, asset_type)
            metadata = fetch_metadata(symbol)
            ticker_id = database.add_ticker(
                symbol=symbol,
                display_symbol=display_symbol,
                asset_type=asset_type,
                name=metadata.name,
                currency=metadata.currency,
                exchange=metadata.exchange,
                path=db_path,
            )
            history = download_history(
                symbol,
                app.config["HISTORY_PERIOD"],
                start=app.config["HISTORY_START_DATE"],
            )
            if history.empty:
                flash(f"No history found for {symbol}. Check the ticker symbol.", "error")
            else:
                saved_rows = database.save_history(ticker_id, history, db_path)
                flash(f"Added {display_symbol} and saved {saved_rows} daily candles.", "success")
        except Exception as exc:
            flash(f"Could not add ticker: {exc}", "error")

        return redirect(url_for("dashboard"))

    @app.post("/tickers/<int:ticker_id>/delete")
    def delete_ticker(ticker_id: int):
        database.delete_ticker(ticker_id, db_path)
        flash("Ticker removed.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tickers/<int:ticker_id>/refresh")
    def refresh_ticker(ticker_id: int):
        ticker = database.get_ticker(ticker_id, db_path)
        if ticker is None:
            flash("Ticker not found.", "error")
            return redirect(url_for("dashboard"))

        try:
            saved_rows = refresh_ticker_if_needed(
                ticker,
                db_path,
                app.config["HISTORY_PERIOD"],
                force=True,
                start=app.config["HISTORY_START_DATE"],
            )
            metadata = fetch_metadata(ticker["symbol"])
            database.update_ticker_metadata(
                ticker_id,
                metadata.name,
                metadata.currency,
                metadata.exchange,
                db_path,
            )
            flash(f"Refreshed {ticker['display_symbol']} with {saved_rows} candles.", "success")
        except Exception as exc:
            flash(f"Could not refresh {ticker['display_symbol']}: {exc}", "error")

        return redirect(url_for("dashboard"))

    @app.post("/tickers/refresh-all")
    def refresh_all_tickers():
        refreshed = 0
        failures = []
        for ticker in database.list_tickers(db_path):
            try:
                refresh_ticker_if_needed(
                    ticker,
                    db_path,
                    app.config["HISTORY_PERIOD"],
                    force=True,
                    start=app.config["HISTORY_START_DATE"],
                )
                refreshed += 1
            except Exception as exc:
                failures.append(f"{ticker['display_symbol']}: {exc}")

        if refreshed:
            flash(f"Refreshed {refreshed} ticker(s).", "success")
        for failure in failures:
            flash(failure, "error")
        return redirect(url_for("dashboard"))

    @app.route("/strategies", methods=["GET", "POST"])
    def strategy_settings():
        configs = database.list_strategy_configs(db_path)
        saved_common_params = common_params(configs)

        if request.method == "POST":
            new_common_params = read_parameter_specs(
                "common",
                COMMON_PARAMETER_SPECS,
                request.form,
            )
            if not new_common_params.get("telegram_bot_token"):
                new_common_params["telegram_bot_token"] = saved_common_params.get(
                    "telegram_bot_token",
                    "",
                )
            database.update_strategy_config(
                COMMON_CONFIG_NAME,
                True,
                new_common_params,
                db_path,
            )
            for strategy_name, strategy in STRATEGIES.items():
                params = read_strategy_params(strategy, request.form)
                enabled = request.form.get(f"{strategy_name}.enabled") == "on"
                database.update_strategy_config(strategy_name, enabled, params, db_path)
            operation_manager.refresh_all(database.list_strategy_configs(db_path))
            flash("Strategy settings saved.", "success")
            return redirect(url_for("strategy_settings"))

        return render_template(
            "strategies.html",
            strategies=STRATEGIES,
            configs=configs,
            common_params=saved_common_params,
            common_parameter_specs=COMMON_PARAMETER_SPECS,
        )

    @app.get("/tickers/<int:ticker_id>/strategies/<strategy_name>/operations")
    def strategy_operations(ticker_id: int, strategy_name: str):
        ticker = database.get_ticker(ticker_id, db_path)
        strategy = STRATEGIES.get(strategy_name)

        if ticker is None:
            flash("Ticker not found.", "error")
            return redirect(url_for("dashboard"))
        if strategy is None:
            flash("Strategy not found.", "error")
            return redirect(url_for("dashboard"))

        configs = database.list_strategy_configs(db_path)
        config = configs.get(
            strategy_name,
            {"enabled": True, "params": strategy.default_params},
        )
        history = strategy_history_for_view(
            database.load_history(ticker_id, db_path),
            strategy_name,
            ticker["asset_type"],
        )
        operations = operation_manager.operations_for(
            ticker_id,
            strategy_name,
            strategy,
            history,
            config["params"],
        )
        moving_average_window = None
        ema_windows = None
        if strategy_name == "turtle_breakout" and config["params"].get(
            "use_ma_filter",
            False,
        ):
            moving_average_window = int(config["params"].get("ma_window", 200))
        if strategy_name == "ema_crossover":
            ema_windows = {
                "fast": int(config["params"].get("fast_window", 12)),
                "slow": int(config["params"].get("slow_window", 26)),
            }
            if config["params"].get("use_trend_filter", False):
                ema_windows["trend"] = int(config["params"].get("trend_ema_window", 200))
        if strategy_name == "macd_trend_following" and config["params"].get(
            "use_trend_filter",
            True,
        ):
            ema_windows = {
                "trend": int(config["params"].get("trend_ema_window", 200)),
            }
        chart = build_operation_chart(
            history,
            operations,
            moving_average_window=moving_average_window,
            ema_windows=ema_windows,
        )

        return render_template(
            "operations.html",
            ticker=ticker,
            strategy=strategy,
            strategy_name=strategy_name,
            config=config,
            operations=operations,
            chart=chart,
        )

    @app.get("/tickers/<int:ticker_id>/strategies/<strategy_name>/backtest")
    def strategy_backtest(ticker_id: int, strategy_name: str):
        ticker = database.get_ticker(ticker_id, db_path)
        strategy = STRATEGIES.get(strategy_name)

        if ticker is None:
            flash("Ticker not found.", "error")
            return redirect(url_for("dashboard"))
        if strategy is None:
            flash("Strategy not found.", "error")
            return redirect(url_for("dashboard"))

        configs = database.list_strategy_configs(db_path)
        config = configs.get(
            strategy_name,
            {"enabled": True, "params": strategy.default_params},
        )
        history = strategy_history_for_view(
            database.load_history(ticker_id, db_path),
            strategy_name,
            ticker["asset_type"],
        )
        operations = operation_manager.operations_for(
            ticker_id,
            strategy_name,
            strategy,
            history,
            config["params"],
        )
        result = yearly_backtest(history, operations)

        return render_template(
            "backtest.html",
            ticker=ticker,
            strategy=strategy,
            strategy_name=strategy_name,
            result=result,
        )

    return app


def evaluate_strategies(
    operations_by_strategy: dict[str, list],
    latest_dates: dict[str, str | None],
    configs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    results = {}
    for strategy_name, strategy in STRATEGIES.items():
        config = configs.get(
            strategy_name,
            {"enabled": True, "params": strategy.default_params},
        )
        if not config["enabled"]:
            results[strategy_name] = {
                "label": strategy.label,
                "signal": "OFF",
                "signal_class": "off",
                "direction": None,
                "operation": None,
                "operation_on_latest_candle": False,
                "detail": "Strategy disabled.",
                "metrics": {},
            }
            continue

        operations = strategy_operations_from_rows(
            operations_by_strategy.get(strategy_name, [])
        )
        latest_date = latest_dates.get(strategy_name)
        if strategy_name == "turtle_breakout":
            results[strategy_name] = turtle_signal_from_operations(
                strategy.label,
                operations,
                latest_date,
            )
        elif strategy_name == "ema_crossover":
            results[strategy_name] = directional_signal_from_operations(
                strategy.label,
                "EMA",
                operations,
                latest_date,
                latest_operation_metrics(operations),
            )
        elif strategy_name == "macd_trend_following":
            results[strategy_name] = directional_signal_from_operations(
                strategy.label,
                "MACD trend",
                operations,
                latest_date,
                latest_operation_metrics(operations),
            )
        else:
            results[strategy_name] = {
                "label": strategy.label,
                "signal": "WAIT",
                "signal_class": "hold",
                "direction": None,
                "operation": None,
                "operation_on_latest_candle": False,
                "detail": "No cached operation history.",
                "metrics": {},
            }
    return results


def strategy_operations_from_rows(rows) -> list:
    from .strategies import StrategyOperation

    return [
        StrategyOperation(
            trade_date=row["trade_date"],
            direction=row["direction"],
            operation=row["operation"],
            price=row["price"],
            signal_price=row["signal_price"],
            detail=row["detail"],
            metrics=json.loads(row["metrics_json"]),
            signal_class=row["signal_class"],
            position_size=row["position_size"],
            position_notional=row["position_notional"],
            realized_pnl=row["realized_pnl"],
            balance_after=row["balance_after"],
        )
        for row in rows
    ]


def latest_operation_metrics(operations) -> dict[str, float | str]:
    return operations[-1].metrics if operations else {}


def latest_strategy_dates(ticker, recent_closes) -> dict[str, str | None]:
    latest_realtime_date = ticker["last_trade_date"]
    completed_date = latest_completed_data_date(ticker["asset_type"]).isoformat()
    latest_completed_date = None
    for close in recent_closes:
        if close["trade_date"] <= completed_date:
            latest_completed_date = close["trade_date"]
            break

    return {
        strategy_name: (
            latest_realtime_date
            if strategy_name == "turtle_breakout"
            else latest_completed_date
        )
        for strategy_name in STRATEGIES
    }


def strategy_history_for_view(history, strategy_name: str, asset_type: str):
    if strategy_name == "turtle_breakout" or history.empty:
        return history

    completed_date = latest_completed_data_date(asset_type)
    return history[history.index.date <= completed_date]


def calculate_daily_change_pct(
    current_price: float | None,
    recent_closes,
    asset_type: str,
    compare_to_latest_close: bool = False,
) -> float | None:
    if current_price is None or not recent_closes:
        return None

    if compare_to_latest_close:
        completed_date = latest_completed_data_date(asset_type).isoformat()
        previous_close = None
        for close in recent_closes:
            if close["trade_date"] <= completed_date:
                previous_close = float(close["close"])
                break
    else:
        latest_close_date = recent_closes[0]["trade_date"]
        previous_close = None
        for close in recent_closes:
            if close["trade_date"] < latest_close_date:
                previous_close = float(close["close"])
                break
    if previous_close is None:
        return None
    if previous_close == 0:
        return None

    change_pct = ((float(current_price) - previous_close) / previous_close) * 100.0

    # Filter provider glitches (for example sudden unit/currency shifts) from dashboard output.
    max_abs_change = 50.0 if asset_type == "crypto" else 30.0
    if abs(change_pct) > max_abs_change:
        return None

    return change_pct


def turtle_signal_from_operations(
    label: str,
    operations,
    latest_date: str | None,
) -> dict[str, Any]:
    operation_on_latest_candle = any(
        operation.trade_date == latest_date for operation in operations
    )
    if operations and operations[-1].trade_date == latest_date:
        latest = operations[-1]
        return {
            "label": label,
            "signal": latest.label,
            "signal_class": latest.signal_class,
            "direction": latest.direction,
            "operation": latest.operation,
            "operation_on_latest_candle": operation_on_latest_candle,
            "detail": latest.detail,
            "metrics": latest.metrics,
        }

    if operations and operations[-1].operation != EXIT:
        latest = operations[-1]
        return {
            "label": label,
            "signal": f"{latest.direction.upper()} HOLD",
            "signal_class": latest.signal_class,
            "direction": latest.direction,
            "operation": None,
            "operation_on_latest_candle": operation_on_latest_candle,
            "detail": f"Holding {latest.direction} Turtle direction.",
            "metrics": latest.metrics,
        }

    return {
        "label": label,
        "signal": "WAIT",
        "signal_class": "hold",
        "direction": None,
        "operation": None,
        "operation_on_latest_candle": operation_on_latest_candle,
        "detail": "Price has not opened a Turtle position.",
        "metrics": {},
    }


def directional_signal_from_operations(
    label: str,
    strategy_label: str,
    operations,
    latest_date: str | None,
    latest_metrics: dict[str, float | str],
) -> dict[str, Any]:
    operation_on_latest_candle = any(
        operation.trade_date == latest_date for operation in operations
    )
    if operations and operations[-1].trade_date == latest_date:
        latest = operations[-1]
        return {
            "label": label,
            "signal": latest.label,
            "signal_class": latest.signal_class,
            "direction": latest.direction,
            "operation": latest.operation,
            "operation_on_latest_candle": operation_on_latest_candle,
            "detail": latest.detail,
            "metrics": latest.metrics or latest_metrics,
        }

    direction = current_direction_from_operations(operations)
    if direction is not None:
        return {
            "label": label,
            "signal": f"{direction.upper()} HOLD",
            "signal_class": direction,
            "direction": direction,
            "operation": None,
            "operation_on_latest_candle": operation_on_latest_candle,
            "detail": f"Holding {direction} {strategy_label} direction.",
            "metrics": latest_metrics,
        }

    return {
        "label": label,
        "signal": "WAIT",
        "signal_class": "hold",
        "direction": None,
        "operation": None,
        "operation_on_latest_candle": operation_on_latest_candle,
        "detail": f"No active {strategy_label} position.",
        "metrics": latest_metrics,
    }


def current_direction_from_operations(operations) -> str | None:
    direction = None
    for operation in operations:
        if operation.operation == "entry":
            direction = operation.direction
        elif operation.operation == "exit" and direction == operation.direction:
            direction = None
    return direction


def group_dashboard_rows(
    rows: list[dict[str, Any]], default_symbols_text: str
) -> list[dict[str, Any]]:
    default_symbols = parse_default_group_symbols(default_symbols_text)
    default_symbol_set = set(default_symbols)
    default_order = {symbol: index for index, symbol in enumerate(default_symbols)}
    groups = {
        "default": {
            "slug": "default",
            "label": "Default",
            "description": (
                ", ".join(default_symbols)
                if default_symbols
                else "No default tickers configured."
            ),
            "rows": [],
        },
        "latest_operations": {
            "slug": "latest-operations",
            "label": "Latest Operations",
            "description": "Tickers with an operation on the latest candle.",
            "rows": [],
        },
        "us_stock": {
            "slug": "us-stock",
            "label": "US-Stock",
            "description": "Tracked US stock tickers.",
            "rows": [],
        },
        "crypto": {
            "slug": "crypto",
            "label": "Crypto",
            "description": "Tracked crypto coins.",
            "rows": [],
        },
    }

    for row in rows:
        ticker = row["ticker"]
        display_symbol = ticker["display_symbol"]
        if display_symbol in default_symbol_set:
            groups["default"]["rows"].append(row)
        if has_latest_operation_marker(row):
            groups["latest_operations"]["rows"].append(row)
        if ticker["asset_type"] == "stock":
            groups["us_stock"]["rows"].append(row)
        elif ticker["asset_type"] == "crypto":
            groups["crypto"]["rows"].append(row)

    groups["default"]["rows"].sort(
        key=lambda row: default_order[row["ticker"]["display_symbol"]]
    )
    return [
        groups["default"],
        groups["latest_operations"],
        groups["us_stock"],
        groups["crypto"],
    ]


def has_latest_operation_marker(row: dict[str, Any]) -> bool:
    return any(
        signal.get("operation_on_latest_candle", False)
        for signal in row.get("strategy_signals", {}).values()
    )


def parse_default_group_symbols(default_symbols_text: str) -> list[str]:
    symbols = []
    seen = set()
    for raw_symbol in str(default_symbols_text).split(","):
        symbol = raw_symbol.strip().upper()
        if not symbol or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)
    return symbols


def page_style(configs: dict[str, dict[str, Any]]) -> str:
    style = str(common_params(configs).get("page_style", "light")).lower()
    if style not in {"light", "dark"}:
        return "light"
    return style


def parse_timezone_offset(value) -> float:
    try:
        offset = float(str(value).strip().replace("UTC", ""))
    except (TypeError, ValueError):
        return 0.0
    return min(14.0, max(-12.0, offset))


def timezone_offset_label(offset: float) -> str:
    sign = "+" if offset >= 0 else "-"
    absolute = abs(offset)
    if absolute.is_integer():
        return f"UTC{sign}{int(absolute)}"
    hours = int(absolute)
    minutes = round((absolute - hours) * 60)
    return f"UTC{sign}{hours}:{minutes:02d}"


def format_dashboard_timestamp(value, timezone_offset: float) -> str | None:
    if not value:
        return None
    raw_value = str(value)
    try:
        timestamp = datetime.fromisoformat(raw_value)
    except ValueError:
        return raw_value
    timestamp += timedelta(hours=timezone_offset)
    return f"{timestamp:%Y-%m-%d %H:%M:%S} {timezone_offset_label(timezone_offset)}"


def read_strategy_params(strategy: TradeStrategy, form) -> dict[str, Any]:
    return read_parameter_specs(strategy.name, strategy.parameters, form)


def read_parameter_specs(prefix: str, parameter_specs, form) -> dict[str, Any]:
    params = {}
    for spec in parameter_specs:
        field_name = f"{prefix}.{spec.name}"
        raw_value = form.get(field_name, spec.default)

        if isinstance(spec.default, bool):
            params[spec.name] = raw_value == "on"
        elif isinstance(spec.default, int):
            params[spec.name] = int(raw_value)
        elif isinstance(spec.default, float):
            params[spec.name] = float(raw_value)
        else:
            params[spec.name] = str(raw_value)

    return params


def access_password_enabled(app: Flask) -> bool:
    return bool(str(app.config.get("ACCESS_PASSWORD", "")).strip())


def is_local_next_url(value: str) -> bool:
    return value.startswith("/") and not value.startswith("//")


if __name__ == "__main__":
    create_app().run(debug=True)
