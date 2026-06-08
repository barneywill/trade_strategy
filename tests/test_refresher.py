import pandas as pd
from datetime import date

from trade_strategy import database
from trade_strategy import refresher
from trade_strategy.common_settings import COMMON_CONFIG_NAME, COMMON_DEFAULTS
from trade_strategy.operation_manager import RealtimeOperationCandidate
from trade_strategy.strategies import ADD_POSITION, ENTRY, LONG, StrategyOperation


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
        "realtime_operation_candidates",
        lambda self, ticker_id, price, configs=None, realtime_date=None: {
            "ema_crossover": [RealtimeOperationCandidate(LONG, ENTRY)]
        },
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
        "realtime_operation_candidates",
        lambda self, ticker_id, price, configs=None, realtime_date=None: {},
    )
    monkeypatch.setattr(
        database,
        "load_strategy_operations",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("operation rows should not load when no realtime candidate")
        ),
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
        "realtime_operation_candidates",
        lambda self, ticker_id, price, configs=None, realtime_date=None: {},
    )

    def download_history(symbol, period):
        calls.append((symbol, period))
        return pd.DataFrame()

    monkeypatch.setattr(refresher, "download_history", download_history)

    result = refresher.refresh_realtime_prices(db_path)
    history = database.load_history(ticker_id, db_path)

    assert result == {"BTC-USD": 104.5}
    assert database.list_current_prices(db_path)[ticker_id]["price"] == 104.5
    assert history.empty
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


def test_force_refresh_rebuilds_operations_when_history_is_unchanged(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("GLDM", "GLDM", "stock", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [10, 11],
            "High": [10, 11],
            "Low": [9, 10],
            "Close": [10, 11],
            "Adj Close": [10, 11],
            "Volume": [1000, 1000],
        },
        index=pd.to_datetime(["2026-06-05", "2026-06-08"]),
    )
    database.save_history(ticker_id, history, db_path)
    calls = []

    monkeypatch.setattr(refresher, "download_history", lambda *args, **kwargs: history)
    monkeypatch.setattr(
        refresher,
        "latest_completed_data_date",
        lambda asset_type: date(2026, 6, 8),
    )

    def refresh_ticker(self, ticker_id, configs=None, asset_type=None, force=False):
        calls.append((ticker_id, asset_type, force))

    monkeypatch.setattr(refresher.OperationManager, "refresh_ticker", refresh_ticker)

    saved_rows = refresher.refresh_ticker_if_needed(
        {
            "id": ticker_id,
            "symbol": "GLDM",
            "asset_type": "stock",
            "last_trade_date": "2026-06-08",
        },
        db_path,
        "2y",
        force=True,
        start="2000-01-01",
    )

    assert saved_rows == 0
    assert calls == [(ticker_id, "stock", True)]


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
    database.update_strategy_config("turtle_breakout", True, {}, db_path)

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
            signal_price = float(history.iloc[-1]["close"]) - 1.0
            return [
                StrategyOperation(
                    trade_date=history.index[-1].date().isoformat(),
                    direction=LONG,
                    operation=ENTRY,
                    price=float(history.iloc[-1]["close"]),
                    signal_price=signal_price,
                    detail="Realtime price triggered entry.",
                    metrics={},
                    signal_class=LONG,
                )
            ]

    sent = []
    current_prices = [108.5, 108.9]
    download_calls = []

    monkeypatch.setattr(refresher, "STRATEGIES", {"turtle_breakout": FakeStrategy()})
    monkeypatch.setattr(
        refresher,
        "current_realtime_data_date",
        lambda asset_type: date(2026, 6, 7),
    )
    monkeypatch.setattr(
        refresher,
        "fetch_current_prices",
        lambda symbols: {"BTC-USD": current_prices.pop(0)},
    )
    monkeypatch.setattr(
        refresher,
        "fetch_current_price",
        lambda symbol: (_ for _ in ()).throw(AssertionError("fallback not expected")),
    )
    monkeypatch.setattr(
        refresher.OperationManager,
        "realtime_operation_candidates",
        lambda self, ticker_id, price, configs=None, realtime_date=None: {
            "turtle_breakout": [RealtimeOperationCandidate(LONG, ENTRY)]
        },
    )

    def download_history(symbol, period):
        download_calls.append((symbol, period))
        return latest_history

    monkeypatch.setattr(refresher, "download_history", download_history)
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
    assert download_calls == [("BTC-USD", "5d")]
    assert database.operation_notification_sent(
        ticker_id,
        "turtle_breakout",
        "2026-06-07|long|entry",
        db_path,
    )


def test_operation_notification_sent_matches_legacy_signal_price_keys(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("INJ-USD", "INJ", "crypto", path=db_path)

    database.mark_operation_notification_sent(
        ticker_id,
        "ema_crossover",
        "2026-06-07|short|exit|5.21800000",
        db_path,
    )

    assert database.operation_notification_sent(
        ticker_id,
        "ema_crossover",
        "2026-06-07|short|exit",
        db_path,
    )


def test_turtle_add_position_candidates_are_deduped_by_signal_price(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("QQQ", "QQQ", "stock", path=db_path)
    database.mark_operation_notification_sent(
        ticker_id,
        "turtle_breakout",
        "2026-06-07|long|add_position|102.00000000",
        db_path,
    )

    previous_keys = {
        "turtle_breakout": {
            "2026-06-07|long|add_position|102.00000000",
        }
    }

    assert not refresher._has_unseen_realtime_candidate(
        ticker_id,
        {
            "turtle_breakout": [
                RealtimeOperationCandidate(LONG, ADD_POSITION, 102.0)
            ]
        },
        date(2026, 6, 7),
        previous_keys,
        db_path,
    )
    assert refresher._has_unseen_realtime_candidate(
        ticker_id,
        {
            "turtle_breakout": [
                RealtimeOperationCandidate(LONG, ADD_POSITION, 103.0)
            ]
        },
        date(2026, 6, 7),
        previous_keys,
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
