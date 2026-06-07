import pandas as pd
import importlib
import sys
import types


sys.modules.pop("trade_strategy.market_data", None)
market_data = importlib.import_module("trade_strategy.market_data")


def test_flatten_history_columns_uses_ohlcv_level():
    columns = pd.MultiIndex.from_tuples(
        [
            ("QQQ", "Open"),
            ("QQQ", "High"),
            ("QQQ", "Low"),
            ("QQQ", "Close"),
            ("QQQ", "Volume"),
        ]
    )

    assert list(market_data._flatten_history_columns(columns)) == [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]


def test_drop_duplicate_history_columns_keeps_scalar_ohlcv_values():
    frame = pd.DataFrame(
        [[1, 2, 3]],
        columns=["Open", "Open", "Close"],
    )
    deduplicated = frame.loc[:, ~frame.columns.duplicated()]

    assert list(deduplicated.columns) == ["Open", "Close"]
    assert deduplicated.iloc[0].get("Open") == 1


def test_latest_close_for_symbol_reads_grouped_batch_history():
    history = pd.DataFrame(
        [
            [100, 200],
            [101, 201],
        ],
        columns=pd.MultiIndex.from_tuples(
            [
                ("SPY", "Close"),
                ("BTC-USD", "Close"),
            ]
        ),
    )

    assert market_data._latest_close_for_symbol(history, "SPY") == 101
    assert market_data._latest_close_for_symbol(history, "BTC-USD") == 201


def test_download_history_retries_transient_download_failure(monkeypatch):
    calls = []
    history = pd.DataFrame(
        {
            "Open": [1],
            "High": [2],
            "Low": [0.5],
            "Close": [1.5],
            "Adj Close": [1.5],
            "Volume": [100],
        },
        index=pd.to_datetime(["2026-06-07"]),
    )

    def download(symbol, **kwargs):
        calls.append((symbol, kwargs))
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        return history

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: None)

    result = market_data.download_history("SPY", "5d")

    assert len(calls) == 2
    assert result["Close"].iloc[-1] == 1.5


def test_fetch_current_prices_retries_batch_failure(monkeypatch):
    calls = []
    history = pd.DataFrame(
        [[100], [101]],
        columns=pd.MultiIndex.from_tuples([("SPY", "Close")]),
    )

    def download(symbols, **kwargs):
        calls.append((symbols, kwargs))
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        return history

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=download))
    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: None)

    assert market_data.fetch_current_prices(["SPY"]) == {"SPY": 101.0}
    assert len(calls) == 2


def test_retry_fetch_sends_alarm_after_attempts_are_exhausted(monkeypatch):
    alarms = []
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("temporary failure")

    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        market_data,
        "_send_fetch_failure_alarm",
        lambda context, error, attempts: alarms.append((context, str(error), attempts)),
    )

    try:
        market_data._retry_fetch(
            fail,
            attempts=3,
            delay_seconds=1,
            alarm_context="Fetch current prices for SPY",
        )
    except RuntimeError:
        pass

    assert len(calls) == 3
    assert alarms == [("Fetch current prices for SPY", "temporary failure", 3)]


def test_fetch_current_price_does_not_alarm_when_history_fallback_succeeds(monkeypatch):
    alarms = []

    class FakeTicker:
        @property
        def fast_info(self):
            raise RuntimeError("fast info unavailable")

        def history(self, **kwargs):
            return pd.DataFrame({"Close": [100.0, 101.5]})

    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        types.SimpleNamespace(Ticker=lambda symbol: FakeTicker()),
    )
    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        market_data,
        "_send_fetch_failure_alarm",
        lambda context, error, attempts: alarms.append((context, str(error), attempts)),
    )

    assert market_data.fetch_current_price("SPY") == 101.5
    assert alarms == []
