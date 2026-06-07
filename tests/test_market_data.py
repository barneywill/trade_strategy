import pandas as pd
import importlib
import sys


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
