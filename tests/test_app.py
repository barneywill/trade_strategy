import sys
import types

import pandas as pd

from trade_strategy import database
from trade_strategy.common_settings import COMMON_CONFIG_NAME
from trade_strategy.operation_manager import OperationManager
from trade_strategy.strategies import EMACrossoverStrategy, STRATEGIES


fake_market_data = types.ModuleType("trade_strategy.market_data")
fake_market_data.download_history = lambda *args, **kwargs: pd.DataFrame()
fake_market_data.fetch_current_price = lambda *args, **kwargs: None
fake_market_data.fetch_current_prices = lambda *args, **kwargs: {}
fake_market_data.fetch_metadata = lambda *args, **kwargs: None
fake_market_data.normalize_symbol = lambda symbol, asset_type: (symbol, symbol)
sys.modules["trade_strategy.market_data"] = fake_market_data

from trade_strategy.app import (  # noqa: E402
    calculate_daily_change_pct,
    create_app,
    group_dashboard_rows,
    parse_default_group_symbols,
)


def test_dashboard_groups_default_symbols_and_market_tabs():
    rows = [
        {
            "ticker": {"display_symbol": "AAPL", "asset_type": "stock"},
            "strategy_signals": {},
        },
        {
            "ticker": {"display_symbol": "ETH", "asset_type": "crypto"},
            "strategy_signals": {
                "ema": {"operation_on_latest_candle": True},
            },
        },
        {
            "ticker": {"display_symbol": "QQQ", "asset_type": "stock"},
            "strategy_signals": {
                "turtle": {"operation_on_latest_candle": True},
            },
        },
        {
            "ticker": {"display_symbol": "DOGE", "asset_type": "crypto"},
            "strategy_signals": {},
        },
        {
            "ticker": {"display_symbol": "MSFT", "asset_type": "stock"},
            "strategy_signals": {
                "ema": {"operation_on_latest_candle": False},
            },
        },
        {
            "ticker": {"display_symbol": "BTC", "asset_type": "crypto"},
            "strategy_signals": {},
        },
    ]

    groups = group_dashboard_rows(rows, "btc, qqq, eth")

    assert [group["label"] for group in groups] == [
        "Default",
        "Latest Operations",
        "US-Stock",
        "Crypto",
    ]
    assert [group["slug"] for group in groups] == [
        "default",
        "latest-operations",
        "us-stock",
        "crypto",
    ]
    assert [
        row["ticker"]["display_symbol"] for row in groups[0]["rows"]
    ] == ["BTC", "QQQ", "ETH"]
    assert [
        row["ticker"]["display_symbol"] for row in groups[1]["rows"]
    ] == ["ETH", "QQQ"]
    assert [
        row["ticker"]["display_symbol"] for row in groups[2]["rows"]
    ] == ["AAPL", "QQQ", "MSFT"]
    assert [
        row["ticker"]["display_symbol"] for row in groups[3]["rows"]
    ] == ["ETH", "DOGE", "BTC"]


def test_default_group_symbol_parser_normalizes_and_deduplicates():
    assert parse_default_group_symbols(" btc, ETH, btc, sol ,, SPY ") == [
        "BTC",
        "ETH",
        "SOL",
        "SPY",
    ]


def test_realtime_daily_change_compares_to_latest_completed_close(monkeypatch):
    monkeypatch.setattr(
        "trade_strategy.app.latest_completed_data_date",
        lambda asset_type: pd.Timestamp("2025-06-10").date(),
    )
    recent_closes = [
        {"trade_date": "2025-06-11", "close": 110},
        {"trade_date": "2025-06-10", "close": 100},
        {"trade_date": "2025-06-09", "close": 80},
    ]

    assert calculate_daily_change_pct(105, recent_closes, "stock", True) == 5.0


def test_access_password_gate_requires_login_when_configured(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
            "ACCESS_PASSWORD": "secret",
            "SECRET_KEY": "test-secret",
        }
    )
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login?next=/")

    response = client.get("/login")
    assert response.status_code == 200
    assert b"Access Required" in response.data

    response = client.post("/login", data={"password": "wrong"})
    assert response.status_code == 200
    assert b"Incorrect password." in response.data

    response = client.post(
        "/login",
        data={"password": "secret", "next": "/strategies"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/strategies")

    response = client.get("/strategies")
    assert response.status_code == 200
    assert b"Log out" in response.data

    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")

    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_app_writes_startup_log_file(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    log_dir = tmp_path / "logs"

    create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
            "LOG_DIR": log_dir,
        }
    )

    log_file = log_dir / "trade_strategy.log"
    assert log_file.exists()
    assert "Trade Strategy app starting" in log_file.read_text()


def test_strategy_settings_renders_and_saves_common_realtime_parameters(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )
    client = app.test_client()

    response = client.get("/strategies")
    assert response.status_code == 200
    assert b"Common" in response.data
    assert b"common.page_style" in response.data
    assert b"value=\"light\"" in response.data
    assert b"common.timezone_offset" in response.data
    assert b"value=\"+0\"" in response.data
    assert b"common.default_group_symbols" in response.data
    assert b"value=\"BTC, ETH, SOL, QQQ, SPY\"" in response.data
    assert b"common.enable_realtime_updates" in response.data
    assert b"common.realtime_update_frequency" in response.data
    assert b"common.daily_data_fetch_time" in response.data
    assert b"value=\"00:01\"" in response.data
    assert b"common.send_telegram_notifications" in response.data
    assert b"common.telegram_bot_token" in response.data
    assert b"common.telegram_chat_id" in response.data
    assert b"value=\"300\"" in response.data

    response = client.post(
        "/strategies",
        data={
            "common.page_style": "dark",
            "common.timezone_offset": "+8",
            "common.default_group_symbols": "QQQ, SPY, BTC",
            "common.enable_realtime_updates": "on",
            "common.realtime_update_frequency": "120",
            "common.daily_data_fetch_time": "00:01",
            "common.send_telegram_notifications": "on",
            "common.telegram_bot_token": "123:abc",
            "common.telegram_chat_id": "456",
            "ema_crossover.enabled": "on",
            "ema_crossover.fast_window": "3",
            "ema_crossover.slow_window": "5",
            "turtle_breakout.enabled": "on",
            "turtle_breakout.entry_window": "20",
            "turtle_breakout.exit_window": "10",
            "turtle_breakout.atr_window": "20",
            "turtle_breakout.exit_atr_ratio": "2.0",
            "turtle_breakout.ma_window": "200",
            "turtle_breakout.max_units": "4",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    config = database.list_strategy_configs(db_path)[COMMON_CONFIG_NAME]
    assert config["params"]["page_style"] == "dark"
    assert config["params"]["timezone_offset"] == "+8"
    assert config["params"]["default_group_symbols"] == "QQQ, SPY, BTC"
    assert config["params"]["enable_realtime_updates"] is True
    assert config["params"]["realtime_update_frequency"] == 120
    assert config["params"]["daily_data_fetch_time"] == "00:01"
    assert config["params"]["send_telegram_notifications"] is True
    assert config["params"]["telegram_bot_token"] == "123:abc"

    response = client.get("/strategies")
    assert b'data-page-style="dark"' in response.data
    assert b'value="dark"' in response.data
    assert b"selected" in response.data
    assert b"placeholder=\"******\"" in response.data
    assert b"value=\"123:abc\"" not in response.data
    assert config["params"]["telegram_chat_id"] == "456"

    response = client.post(
        "/strategies",
        data={
            "common.page_style": "dark",
            "common.timezone_offset": "+8",
            "common.default_group_symbols": "QQQ, SPY, BTC",
            "common.enable_realtime_updates": "on",
            "common.realtime_update_frequency": "120",
            "common.daily_data_fetch_time": "00:01",
            "common.send_telegram_notifications": "on",
            "common.telegram_bot_token": "",
            "common.telegram_chat_id": "456",
            "ema_crossover.enabled": "on",
            "ema_crossover.fast_window": "3",
            "ema_crossover.slow_window": "5",
            "turtle_breakout.enabled": "on",
            "turtle_breakout.entry_window": "20",
            "turtle_breakout.exit_window": "10",
            "turtle_breakout.atr_window": "20",
            "turtle_breakout.exit_atr_ratio": "2.0",
            "turtle_breakout.ma_window": "200",
            "turtle_breakout.max_units": "4",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    config = database.list_strategy_configs(db_path)[COMMON_CONFIG_NAME]
    assert config["params"]["telegram_bot_token"] == "123:abc"


def test_dashboard_uses_cached_realtime_price_when_enabled(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )
    ticker_id = database.add_ticker("SPY", "SPY", "stock", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [100],
            "High": [101],
            "Low": [99],
            "Close": [100],
            "Adj Close": [100],
            "Volume": [1000],
        },
        index=pd.date_range("2025-01-02", periods=1),
    )
    database.save_history(ticker_id, history, db_path)
    database.save_current_price(ticker_id, 123.45, db_path)
    database.update_strategy_config(
        COMMON_CONFIG_NAME,
        True,
        {
            "enable_realtime_updates": True,
            "realtime_update_frequency": 300,
        },
        db_path,
    )

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"123.45" in response.data
    assert b"Updated" in response.data
    assert b"role=\"tablist\"" in response.data
    assert b"data-group-tab=\"default\"" in response.data
    assert b"data-group-tab=\"latest-operations\"" in response.data
    assert b"data-group-panel=\"latest-operations\"" in response.data
    assert b"data-group-panel=\"us-stock\"" in response.data
    assert b"hidden" in response.data
    assert b"data-dashboard-auto-refresh" in response.data
    assert b"<option value=\"0\">Never</option>" in response.data
    assert b"<option value=\"300000\">5 min</option>" in response.data
    assert b"<option value=\"600000\">10 min</option>" in response.data
    assert b"tradeStrategyDashboardAutoRefreshMs" in response.data
    assert b"window.location.reload()" in response.data


def test_dashboard_displays_times_with_common_timezone_offset(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )
    ticker_id = database.add_ticker("SPY", "SPY", "stock", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [100],
            "High": [101],
            "Low": [99],
            "Close": [100],
            "Adj Close": [100],
            "Volume": [1000],
        },
        index=pd.date_range("2025-01-02", periods=1),
    )
    database.save_history(ticker_id, history, db_path)
    database.save_current_price(ticker_id, 123.45, db_path)
    with database.connect(db_path) as connection:
        connection.execute(
            "UPDATE current_prices SET updated_at = ? WHERE ticker_id = ?",
            ("2026-06-11 02:30:00", ticker_id),
        )
        connection.execute(
            "UPDATE tickers SET last_downloaded_at = ? WHERE id = ?",
            ("2026-06-11 01:15:00", ticker_id),
        )
    database.update_strategy_config(
        COMMON_CONFIG_NAME,
        True,
        {
            "enable_realtime_updates": True,
            "timezone_offset": "+8",
        },
        db_path,
    )

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"Updated 2026-06-11 10:30:00 UTC+8" in response.data
    assert b"Downloaded 2026-06-11 09:15:00 UTC+8" in response.data


def test_dashboard_links_tickers_to_yahoo_charts(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )
    spy_id = database.add_ticker("SPY", "SPY", "stock", path=db_path)
    btc_id = database.add_ticker("BTC-USD", "BTC", "crypto", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [100],
            "High": [101],
            "Low": [99],
            "Close": [100],
            "Adj Close": [100],
            "Volume": [1000],
        },
        index=pd.date_range("2025-01-02", periods=1),
    )
    database.save_history(spy_id, history, db_path)
    database.save_history(btc_id, history, db_path)

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b'href="https://finance.yahoo.com/chart/SPY"' in response.data
    assert b'href="https://finance.yahoo.com/chart/BTC-USD"' in response.data
    assert b'target="_blank"' in response.data
    assert b'rel="noopener noreferrer"' in response.data


def test_dashboard_marks_strategy_with_operation_on_latest_candle(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )
    ticker_id = database.add_ticker("SPY", "SPY", "stock", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [10, 9, 8, 9, 12],
            "High": [10, 9, 8, 9, 12],
            "Low": [10, 9, 8, 9, 12],
            "Close": [10, 9, 8, 9, 12],
            "Adj Close": [10, 9, 8, 9, 12],
            "Volume": [1000] * 5,
        },
        index=pd.date_range(end=pd.Timestamp("2026-06-05"), periods=5),
    )
    database.save_history(ticker_id, history, db_path)
    database.update_strategy_config(
        "ema_crossover",
        True,
        {
            "fast_window": 2,
            "slow_window": 3,
        },
        db_path,
    )
    database.update_strategy_config("turtle_breakout", False, {}, db_path)
    OperationManager(db_path).operations_for(
        ticker_id,
        "ema_crossover",
        STRATEGIES["ema_crossover"],
        database.load_history(ticker_id, db_path),
        {
            "fast_window": 2,
            "slow_window": 3,
        },
    )

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"latest-operation-marker" in response.data
    assert b"title=\"Operation on latest candle\"" in response.data
    assert (
        f'href="/tickers/{ticker_id}/strategies/ema_crossover/operations"'.encode()
        in response.data
    )
    assert b'target="_blank"' in response.data
    assert b'rel="noopener noreferrer"' in response.data


def test_dashboard_uses_cached_ema_operations_without_recomputing_evaluate(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )
    ticker_id = database.add_ticker("SPY", "SPY", "stock", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [14, 13, 12, 11, 10, 11, 13, 15],
            "High": [14, 13, 12, 11, 10, 11, 13, 15],
            "Low": [14, 13, 12, 11, 10, 11, 13, 15],
            "Close": [14, 13, 12, 11, 10, 11, 13, 15],
            "Adj Close": [14, 13, 12, 11, 10, 11, 13, 15],
            "Volume": [1000] * 8,
        },
        index=pd.date_range("2026-05-18", periods=8),
    )
    database.save_history(ticker_id, history, db_path)
    params = {"fast_window": 2, "slow_window": 4}
    database.update_strategy_config("ema_crossover", True, params, db_path)
    database.update_strategy_config("macd_trend_following", False, {}, db_path)
    database.update_strategy_config("turtle_breakout", False, {}, db_path)
    OperationManager(db_path).operations_for(
        ticker_id,
        "ema_crossover",
        STRATEGIES["ema_crossover"],
        database.load_history(ticker_id, db_path),
        params,
    )

    def fail_evaluate(self, history, params):
        raise AssertionError("dashboard should use cached EMA operations")

    monkeypatch.setattr(EMACrossoverStrategy, "evaluate", fail_evaluate)

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"EMA Crossover" in response.data
    assert b"HOLD" in response.data


def test_dashboard_uses_cached_summaries_without_loading_full_history(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )
    ticker_id = database.add_ticker("SPY", "SPY", "stock", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [10, 11],
            "High": [10, 11],
            "Low": [10, 11],
            "Close": [10, 11],
            "Adj Close": [10, 11],
            "Volume": [1000, 1000],
        },
        index=pd.to_datetime(["2026-05-28", "2026-05-29"]),
    )
    database.save_history(ticker_id, history, db_path)
    database.update_strategy_config("ema_crossover", False, {}, db_path)
    database.update_strategy_config("macd_trend_following", False, {}, db_path)
    database.update_strategy_config("turtle_breakout", False, {}, db_path)

    monkeypatch.setattr(
        database,
        "load_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dashboard should not load full history")
        ),
    )

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"SPY" in response.data


def test_operations_page_renders_open_cycle_chart_dots(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )

    ticker_id = database.add_ticker(
        "QQQ",
        "QQQ",
        "stock",
        path=db_path,
    )
    history = pd.DataFrame(
        {
            "Open": range(100, 490),
            "High": range(100, 490),
            "Low": range(99, 489),
            "Close": range(100, 490),
            "Adj Close": range(100, 490),
            "Volume": [1000] * 390,
        },
        index=pd.date_range("2025-05-12", periods=390),
    )
    database.save_history(ticker_id, history, db_path)
    database.update_strategy_config(
        "turtle_breakout",
        True,
        {
            "entry_window": 3,
            "exit_window": 2,
            "atr_window": 3,
            "exit_atr_ratio": 2.0,
            "use_ma_filter": True,
            "ma_window": 3,
            "max_units": 4,
        },
        db_path,
    )

    response = app.test_client().get(
        f"/tickers/{ticker_id}/strategies/turtle_breakout/operations"
    )

    assert response.status_code == 200
    assert b"class=\"chart-dot long\"" in response.data
    assert b"class=\"chart-line moving-average-line\"" in response.data
    assert b"MA 3" in response.data
    assert response.data.count(b"class=\"chart-dot long\"") == 4
    assert b"class=\"metrics-cell\"" in response.data
    assert b"class=\"metrics-details\"" in response.data
    assert b"<summary>" in response.data


def test_ema_operations_page_renders_fast_and_slow_ema_lines(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": db_path,
            "AUTO_REFRESH_ENABLED": False,
        }
    )

    ticker_id = database.add_ticker(
        "SPY",
        "SPY",
        "stock",
        path=db_path,
    )
    history = pd.DataFrame(
        {
            "Open": range(100, 130),
            "High": range(101, 131),
            "Low": range(99, 129),
            "Close": range(100, 130),
            "Adj Close": range(100, 130),
            "Volume": [1000] * 30,
        },
        index=pd.date_range("2025-01-01", periods=30),
    )
    database.save_history(ticker_id, history, db_path)
    database.update_strategy_config(
        "ema_crossover",
        True,
        {
            "fast_window": 3,
            "slow_window": 5,
        },
        db_path,
    )

    response = app.test_client().get(
        f"/tickers/{ticker_id}/strategies/ema_crossover/operations"
    )

    assert response.status_code == 200
    assert b"class=\"chart-line fast-ema-line\"" in response.data
    assert b"class=\"chart-line slow-ema-line\"" in response.data
    assert b"Fast EMA 3" in response.data
    assert b"Slow EMA 5" in response.data
