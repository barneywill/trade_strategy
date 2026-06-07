import pandas as pd
from datetime import date

from trade_strategy import database
from trade_strategy import refresher
from trade_strategy.common_settings import COMMON_CONFIG_NAME, COMMON_DEFAULTS
from trade_strategy.strategies import ENTRY, LONG, StrategyOperation


def test_realtime_refresh_updates_crypto_latest_daily_candle(tmp_path, monkeypatch):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("BTC-USD", "BTC", "crypto", path=db_path)

    latest_history = pd.DataFrame(
        {
            "Open": [100],
            "High": [105],
            "Low": [99],
            "Close": [104],
            "Adj Close": [104],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-06-07"]),
    )
    monkeypatch.setattr(refresher, "fetch_current_prices", lambda symbols: {"BTC-USD": 104.5})
    monkeypatch.setattr(
        refresher,
        "current_realtime_data_date",
        lambda asset_type: date(2026, 6, 7),
    )
    monkeypatch.setattr(
        refresher,
        "fetch_current_price",
        lambda symbol: (_ for _ in ()).throw(AssertionError("fallback not expected")),
    )
    monkeypatch.setattr(
        refresher.OperationManager,
        "realtime_operation_triggered",
        lambda self, ticker_id, price, configs=None: True,
    )
    monkeypatch.setattr(refresher, "download_history", lambda symbol, period: latest_history)

    result = refresher.refresh_realtime_prices(db_path)
    history = database.load_history(ticker_id, db_path)
    prices = database.list_current_prices(db_path)

    assert result == {"BTC-USD": 104.5}
    assert history.index[-1].date().isoformat() == "2026-06-07"
    assert history["high"].iloc[-1] == 105
    assert history["low"].iloc[-1] == 99
    assert history["close"].iloc[-1] == 104.5
    assert prices[ticker_id]["price"] == 104.5


def test_realtime_refresh_batches_eligible_tickers(tmp_path, monkeypatch):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    spy_id = database.add_ticker("SPY", "SPY", "stock", path=db_path)
    btc_id = database.add_ticker("BTC-USD", "BTC", "crypto", path=db_path)
    seen_batches = []

    latest_history = pd.DataFrame(
        {
            "Open": [100],
            "High": [105],
            "Low": [99],
            "Close": [104],
            "Adj Close": [104],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-06-07"]),
    )
    monkeypatch.setattr(refresher, "is_us_stock_market_open", lambda: True)

    def fetch_current_prices(symbols):
        seen_batches.append(symbols)
        return {"SPY": 500.25, "BTC-USD": 104.5}

    monkeypatch.setattr(refresher, "fetch_current_prices", fetch_current_prices)
    monkeypatch.setattr(
        refresher,
        "fetch_current_price",
        lambda symbol: (_ for _ in ()).throw(AssertionError("fallback not expected")),
    )
    monkeypatch.setattr(
        refresher.OperationManager,
        "realtime_operation_triggered",
        lambda self, ticker_id, price, configs=None: False,
    )
    monkeypatch.setattr(refresher, "download_history", lambda symbol, period: latest_history)

    result = refresher.refresh_realtime_prices(db_path)
    prices = database.list_current_prices(db_path)

    assert len(seen_batches) == 1
    assert set(seen_batches[0]) == {"SPY", "BTC-USD"}
    assert result == {"SPY": 500.25, "BTC-USD": 104.5}
    assert prices[spy_id]["price"] == 500.25
    assert prices[btc_id]["price"] == 104.5


def test_realtime_refresh_skips_history_download_without_operation_trigger(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("BTC-USD", "BTC", "crypto", path=db_path)
    calls = []

    monkeypatch.setattr(refresher, "fetch_current_prices", lambda symbols: {"BTC-USD": 104.5})
    monkeypatch.setattr(
        refresher,
        "current_realtime_data_date",
        lambda asset_type: date(2026, 6, 7),
    )
    monkeypatch.setattr(
        refresher,
        "fetch_current_price",
        lambda symbol: (_ for _ in ()).throw(AssertionError("fallback not expected")),
    )
    monkeypatch.setattr(
        refresher.OperationManager,
        "realtime_operation_triggered",
        lambda self, ticker_id, price, configs=None: False,
    )

    def download_history(symbol, period):
        calls.append((symbol, period))
        return pd.DataFrame()

    monkeypatch.setattr(refresher, "download_history", download_history)

    result = refresher.refresh_realtime_prices(db_path)
    history = database.load_history(ticker_id, db_path)

    assert result == {"BTC-USD": 104.5}
    assert database.list_current_prices(db_path)[ticker_id]["price"] == 104.5
    assert history.index[-1].date().isoformat() == "2026-06-07"
    assert history["open"].iloc[-1] == 104.5
    assert history["high"].iloc[-1] == 104.5
    assert history["low"].iloc[-1] == 104.5
    assert history["close"].iloc[-1] == 104.5
    assert calls == []


def test_save_history_reports_only_changed_rows(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("BTC-USD", "BTC", "crypto", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [100],
            "High": [105],
            "Low": [99],
            "Close": [104],
            "Adj Close": [104],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-06-07"]),
    )

    assert database.save_history(ticker_id, history, db_path) == 1
    assert database.save_history(ticker_id, history, db_path) == 0
    assert (
        database.save_history(
            ticker_id,
            history.assign(Close=[104.5], **{"Adj Close": [104.5]}),
            db_path,
        )
        == 1
    )


def test_realtime_refresh_skips_stock_candles_when_market_is_closed(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("QQQ", "QQQ", "stock", path=db_path)
    calls = []

    monkeypatch.setattr(refresher, "is_us_stock_market_open", lambda: False)
    monkeypatch.setattr(refresher, "fetch_current_prices", lambda symbols: {})
    monkeypatch.setattr(refresher, "fetch_current_price", lambda symbol: 500.0)

    def download_history(symbol, period):
        calls.append((symbol, period))
        return pd.DataFrame()

    monkeypatch.setattr(refresher, "download_history", download_history)

    result = refresher.refresh_realtime_prices(db_path)
    history = database.load_history(ticker_id, db_path)

    assert result == {"QQQ": None}
    assert calls == []
    assert history.empty


def test_realtime_refresh_sends_telegram_for_new_operation_once(tmp_path, monkeypatch):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("BTC-USD", "BTC", "crypto", path=db_path)
    database.update_strategy_config(
        COMMON_CONFIG_NAME,
        True,
        {
            **COMMON_DEFAULTS,
            "send_telegram_notifications": True,
            "telegram_bot_token": "123:abc",
            "telegram_chat_id": "456",
        },
        db_path,
    )
    database.update_strategy_config("fake_strategy", True, {}, db_path)

    latest_history = pd.DataFrame(
        {
            "Open": [100],
            "High": [110],
            "Low": [99],
            "Close": [108],
            "Adj Close": [108],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-06-07"]),
    )

    class FakeStrategy:
        label = "Fake Strategy"
        default_params = {}

        def operation_history(self, history, params):
            return [
                StrategyOperation(
                    trade_date=history.index[-1].date().isoformat(),
                    direction=LONG,
                    operation=ENTRY,
                    price=108.0,
                    signal_price=107.0,
                    detail="Realtime price triggered entry.",
                    metrics={},
                    signal_class=LONG,
                )
            ]

    sent = []

    monkeypatch.setattr(refresher, "STRATEGIES", {"fake_strategy": FakeStrategy()})
    monkeypatch.setattr(refresher, "fetch_current_prices", lambda symbols: {"BTC-USD": 108.5})
    monkeypatch.setattr(
        refresher,
        "fetch_current_price",
        lambda symbol: (_ for _ in ()).throw(AssertionError("fallback not expected")),
    )
    monkeypatch.setattr(
        refresher.OperationManager,
        "realtime_operation_triggered",
        lambda self, ticker_id, price, configs=None: True,
    )
    monkeypatch.setattr(refresher, "download_history", lambda symbol, period: latest_history)
    monkeypatch.setattr(
        refresher,
        "send_operation_notification",
        lambda config, ticker, strategy_label, operation: sent.append(
            (ticker["display_symbol"], strategy_label, operation.label)
        )
        or True,
    )

    refresher.refresh_realtime_prices(db_path)
    refresher.refresh_realtime_prices(db_path)

    assert sent == [("BTC", "Fake Strategy", "LONG ENTRY")]
    assert database.operation_notification_sent(
        ticker_id,
        "fake_strategy",
        "2026-06-07|long|entry|107.00000000",
        db_path,
    )


def test_refresh_due_tickers_force_is_optional(tmp_path, monkeypatch):
    db_path = tmp_path / "trade_strategy.sqlite3"
    seen_force_values = []
    tickers = [
        {
            "id": 1,
            "symbol": "BTC-USD",
            "asset_type": "crypto",
            "last_trade_date": "2026-06-07",
        }
    ]

    monkeypatch.setattr(refresher.database, "list_tickers", lambda path: tickers)

    def refresh_ticker_if_needed(ticker, db_path, period, force=False, start=None):
        seen_force_values.append(force)
        return 0

    monkeypatch.setattr(
        refresher,
        "refresh_ticker_if_needed",
        refresh_ticker_if_needed,
    )

    refresher.refresh_due_tickers(db_path, "1mo")
    refresher.refresh_due_tickers(db_path, "1mo", force=True)

    assert seen_force_values == [False, True]


def test_daily_data_fetch_time_parser():
    assert refresher._parse_daily_data_fetch_time("00:01") == (0, 1)
    assert refresher._parse_daily_data_fetch_time("23:59") == (23, 59)
    assert refresher._parse_daily_data_fetch_time("25:00") == (0, 1)
    assert refresher._parse_daily_data_fetch_time("not-a-time") == (0, 1)
