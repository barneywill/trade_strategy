from datetime import date

import pandas as pd

from trade_strategy import database
from trade_strategy import operation_manager as operation_manager_module
from trade_strategy.operation_manager import OperationManager, RealtimeOperationCandidate
from trade_strategy.strategies import STRATEGIES
from trade_strategy.strategies import (
    ADD_POSITION,
    ENTRY,
    LONG,
    StrategyOperation,
    TurtleBreakoutStrategy,
)


class CountingStrategy:
    def __init__(self):
        self.calls = 0
        self.last_dates = []

    def operation_history(self, history, params):
        self.calls += 1
        self.last_dates.append(history.index[-1].date().isoformat())
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


def test_operation_manager_force_bypasses_operation_cache(tmp_path):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("GLDM", "GLDM", "stock", path=db_path)
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
        {"value": 1},
        force=True,
    )

    assert strategy.calls == 2
    rows = database.load_strategy_operations(ticker_id, "counting", db_path)
    assert rows[-1]["detail"] == "call 2"


def test_refresh_ticker_uses_completed_history_for_non_turtle_strategies(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "trade_strategy.sqlite3"
    database.init_db(db_path)
    ticker_id = database.add_ticker("BTC-USD", "BTC", "crypto", path=db_path)
    history = pd.DataFrame(
        {
            "Open": [100, 105],
            "High": [101, 106],
            "Low": [99, 104],
            "Close": [100, 105],
            "Adj Close": [100, 105],
            "Volume": [1000, 1000],
        },
        index=pd.to_datetime(["2026-06-07", "2026-06-08"]),
    )
    database.save_history(ticker_id, history, db_path)
    ema_strategy = CountingStrategy()
    turtle_strategy = CountingStrategy()
    monkeypatch.setattr(
        operation_manager_module,
        "latest_completed_data_date",
        lambda asset_type: date(2026, 6, 7),
    )
    monkeypatch.setattr(
        operation_manager_module,
        "STRATEGIES",
        {
            "ema_crossover": ema_strategy,
            "turtle_breakout": turtle_strategy,
        },
    )
    manager = OperationManager(db_path)

    manager.refresh_ticker(
        ticker_id,
        {
            "ema_crossover": {"enabled": True, "params": {"value": 1}},
            "turtle_breakout": {"enabled": True, "params": {"value": 1}},
        },
        asset_type="crypto",
    )

    assert ema_strategy.last_dates == ["2026-06-07"]
    assert turtle_strategy.last_dates == ["2026-06-08"]


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
    assert manager.realtime_operation_candidates(ticker_id, 101.5, configs) == {
        "turtle_breakout": [RealtimeOperationCandidate(LONG, ENTRY)]
    }


def test_realtime_candidates_only_include_turtle_strategy(tmp_path):
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
    configs = {
        name: {"enabled": True, "params": strategy.default_params}
        for name, strategy in STRATEGIES.items()
    }
    configs["turtle_breakout"]["params"] = {
        **STRATEGIES["turtle_breakout"].default_params,
        "entry_window": 20,
        "exit_window": 10,
        "atr_window": 20,
        "use_ma_filter": False,
    }

    assert set(manager.realtime_operation_candidates(ticker_id, 101.5, configs)) == {
        "turtle_breakout"
    }


def test_turtle_realtime_candidate_can_enter_and_add_multiple_units_same_day(tmp_path):
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
        "max_units": 4,
        "use_ma_filter": False,
    }
    configs = {
        "turtle_breakout": {"enabled": True, "params": params},
        "ema_crossover": {
            "enabled": False,
            "params": STRATEGIES["ema_crossover"].default_params,
        },
        "macd_trend_following": {
            "enabled": False,
            "params": STRATEGIES["macd_trend_following"].default_params,
        },
    }

    assert manager.realtime_operation_candidates(
        ticker_id,
        107.0,
        configs,
        date(2026, 5, 31),
    ) == {
        "turtle_breakout": [
            RealtimeOperationCandidate(LONG, ENTRY),
            RealtimeOperationCandidate(LONG, ADD_POSITION, 102.0),
            RealtimeOperationCandidate(LONG, ADD_POSITION, 103.0),
            RealtimeOperationCandidate(LONG, ADD_POSITION, 104.0),
        ]
    }


def test_realtime_trigger_uses_stable_completed_history_snapshot(tmp_path, monkeypatch):
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

    class CountingTurtleStrategy(TurtleBreakoutStrategy):
        def __init__(self):
            self.calls = 0

        def operation_history(self, history, params):
            self.calls += 1
            return super().operation_history(history, params)

    strategy = CountingTurtleStrategy()
    monkeypatch.setattr(
        operation_manager_module,
        "STRATEGIES",
        {"turtle_breakout": strategy},
    )
    params = {
        **strategy.default_params,
        "entry_window": 20,
        "exit_window": 10,
        "atr_window": 20,
        "use_ma_filter": False,
    }
    configs = {
        "turtle_breakout": {"enabled": True, "params": params},
        "ema_crossover": {
            "enabled": False,
            "params": STRATEGIES["ema_crossover"].default_params,
        },
        "macd_trend_following": {
            "enabled": False,
            "params": STRATEGIES["macd_trend_following"].default_params,
        },
    }
    manager = OperationManager(db_path)

    database.save_realtime_candle(ticker_id, date(2026, 5, 31), 100.5, db_path)
    manager.realtime_operation_triggered(
        ticker_id,
        100.5,
        configs,
        date(2026, 5, 31),
    )
    database.save_realtime_candle(ticker_id, date(2026, 5, 31), 100.8, db_path)
    manager.realtime_operation_triggered(
        ticker_id,
        100.8,
        configs,
        date(2026, 5, 31),
    )

    assert strategy.calls == 1


def test_realtime_candidate_reuses_cached_turtle_expression_without_db_reads(
    tmp_path,
    monkeypatch,
):
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
        "turtle_breakout": {"enabled": True, "params": params},
        "ema_crossover": {
            "enabled": False,
            "params": STRATEGIES["ema_crossover"].default_params,
        },
        "macd_trend_following": {
            "enabled": False,
            "params": STRATEGIES["macd_trend_following"].default_params,
        },
    }

    assert manager.realtime_operation_candidates(
        ticker_id,
        101.5,
        configs,
        date(2026, 5, 31),
    )

    monkeypatch.setattr(
        operation_manager_module.database,
        "load_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cached expression should avoid loading history")
        ),
    )
    monkeypatch.setattr(
        OperationManager,
        "_load_operations",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cached expression should avoid loading operations")
        ),
    )

    assert manager.realtime_operation_candidates(
        ticker_id,
        101.6,
        configs,
        date(2026, 5, 31),
    )


def test_turtle_realtime_add_candidate_uses_saved_current_day_units(tmp_path):
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
    database.save_strategy_operations(
        ticker_id,
        "turtle_breakout",
        [
            StrategyOperation(
                trade_date="2026-05-30",
                direction=LONG,
                operation=ENTRY,
                price=101.0,
                signal_price=101.0,
                detail="Long entry.",
                metrics={"atr": 2.0},
                signal_class=LONG,
            ),
            StrategyOperation(
                trade_date="2026-05-31",
                direction=LONG,
                operation=ADD_POSITION,
                price=102.5,
                signal_price=102.0,
                detail="Long add.",
                metrics={"atr": 2.0},
                signal_class=LONG,
            ),
        ],
        "saved-current-day-add",
        db_path,
    )
    manager = OperationManager(db_path)
    params = {
        **STRATEGIES["turtle_breakout"].default_params,
        "entry_window": 20,
        "exit_window": 10,
        "atr_window": 20,
        "use_ma_filter": False,
    }
    configs = {
        "turtle_breakout": {"enabled": True, "params": params},
        "ema_crossover": {
            "enabled": False,
            "params": STRATEGIES["ema_crossover"].default_params,
        },
        "macd_trend_following": {
            "enabled": False,
            "params": STRATEGIES["macd_trend_following"].default_params,
        },
    }

    assert manager.realtime_operation_candidates(
        ticker_id,
        103.5,
        configs,
        date(2026, 5, 31),
    ) == {
        "turtle_breakout": [
            RealtimeOperationCandidate(LONG, ADD_POSITION, 103.0)
        ]
    }
