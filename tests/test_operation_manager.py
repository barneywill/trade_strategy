import pandas as pd

from trade_strategy import database
from trade_strategy.operation_manager import OperationManager
from trade_strategy.strategies import STRATEGIES
from trade_strategy.strategies import ENTRY, LONG, StrategyOperation


class CountingStrategy:
    def __init__(self):
        self.calls = 0

    def operation_history(self, history, params):
        self.calls += 1
        close = float(history["close"].iloc[-1])
        return [
            StrategyOperation(
                trade_date=history.index[-1].date().isoformat(),
                direction=LONG,
                operation=ENTRY,
                price=close,
                signal_price=close,
                detail=f"call {self.calls}",
                metrics={"value": params["value"]},
                signal_class=LONG,
            )
        ]


def test_operation_manager_caches_and_invalidates_operations(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("SPY", "SPY", "stock", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [10],
            "High": [11],
            "Low": [9],
            "Close": [10],
            "Adj Close": [10],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-06-05"]),
    )
    database.save_history(ticker_id, history, db_path)
    strategy = CountingStrategy()
    manager = OperationManager(db_path)

    operations = manager.operations_for(
        ticker_id,
        "counting",
        strategy,
        database.load_history(ticker_id, db_path),
        {"value": 1},
    )
    cached_operations = manager.operations_for(
        ticker_id,
        "counting",
        strategy,
        database.load_history(ticker_id, db_path),
        {"value": 1},
    )

    assert strategy.calls == 1
    assert operations == cached_operations
    assert database.load_strategy_operations(ticker_id, "counting", db_path)

    database.save_history(
        ticker_id,
        history.assign(Close=[12], **{"Adj Close": [12]}),
        db_path,
    )
    manager.operations_for(
        ticker_id,
        "counting",
        strategy,
        database.load_history(ticker_id, db_path),
        {"value": 1},
    )
    manager.operations_for(
        ticker_id,
        "counting",
        strategy,
        database.load_history(ticker_id, db_path),
        {"value": 2},
    )

    assert strategy.calls == 3


def test_operation_manager_detects_turtle_realtime_entry_trigger(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("QQQ", "QQQ", "stock", path=db_path)
    dates = pd.date_range("2026-05-01", periods=30, freq="D")
    history = pd.DataFrame(
        {
            "Open": [100] * 30,
            "High": [101] * 30,
            "Low": [99] * 30,
            "Close": [100] * 30,
            "Adj Close": [100] * 30,
            "Volume": [1000] * 30,
        },
        index=dates,
    )
    database.save_history(ticker_id, history, db_path)
    manager = OperationManager(db_path)
    params = {
        **STRATEGIES["turtle_breakout"].default_params,
        "entry_window": 20,
        "exit_window": 10,
        "atr_window": 20,
        "use_ma_filter": False,
    }
    configs = {
        "turtle_breakout": {
            "enabled": True,
            "params": params,
        },
        "ema_crossover": {
            "enabled": False,
            "params": STRATEGIES["ema_crossover"].default_params,
        },
        "macd_trend_following": {
            "enabled": False,
            "params": STRATEGIES["macd_trend_following"].default_params,
        },
    }

    assert not manager.realtime_operation_triggered(ticker_id, 100.5, configs)
    assert manager.realtime_operation_triggered(ticker_id, 101.5, configs)
