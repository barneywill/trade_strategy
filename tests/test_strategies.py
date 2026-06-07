import pandas as pd

from trade_strategy.strategies import (
    EMACrossoverStrategy,
    MACDTrendFollowingStrategy,
    TurtleBreakoutStrategy,
)


def test_ema_strategy_returns_long_hold_when_fast_remains_above_slow():
    history = pd.DataFrame(
        {"close": [10, 10, 10, 11, 12, 13, 14, 15]},
        index=pd.date_range("2024-01-01", periods=8),
    )

    result = EMACrossoverStrategy().evaluate(
        history,
        {"fast_window": 2, "slow_window": 4},
    )

    assert result.signal == "LONG HOLD"
    assert result.direction == "long"
    assert result.operation is None
    assert result.signal_class == "long"
    assert "fast_ema" in result.metrics


def test_ema_strategy_returns_short_hold_when_fast_remains_below_slow():
    history = pd.DataFrame(
        {"close": [15, 15, 15, 14, 13, 12, 11, 10]},
        index=pd.date_range("2024-01-01", periods=8),
    )

    result = EMACrossoverStrategy().evaluate(
        history,
        {"fast_window": 2, "slow_window": 4},
    )

    assert result.signal == "SHORT HOLD"
    assert result.direction == "short"
    assert result.operation is None
    assert result.signal_class == "short"


def test_ema_strategy_operation_history_lists_directional_entries_and_exits():
    history = pd.DataFrame(
        {"close": [14, 13, 12, 11, 10, 11, 13, 15, 14, 12, 10, 8]},
        index=pd.date_range("2024-01-01", periods=12),
    )

    operations = EMACrossoverStrategy().operation_history(
        history,
        {"fast_window": 2, "slow_window": 4},
    )

    assert [(operation.direction, operation.operation) for operation in operations] == [
        ("short", "entry"),
        ("short", "exit"),
        ("long", "entry"),
        ("long", "exit"),
        ("short", "entry"),
    ]
    assert all(operation.trade_date for operation in operations)
    assert all(operation.price > 0 for operation in operations)
    assert operations[0].position_notional == 10000.0
    assert operations[0].position_size == 909.09090909
    assert operations[1].realized_pnl == -1818.18
    assert operations[1].balance_after == 8181.82


def test_ema_trend_filter_blocks_counter_trend_long_entry():
    history = pd.DataFrame(
        {"close": [100, 100, 100, 10, 20, 30, 40, 50]},
        index=pd.date_range("2024-01-01", periods=8),
    )

    operations = EMACrossoverStrategy().operation_history(
        history,
        {
            "fast_window": 2,
            "slow_window": 4,
            "use_trend_filter": True,
            "trend_ema_window": 8,
        },
    )
    result = EMACrossoverStrategy().evaluate(
        history,
        {
            "fast_window": 2,
            "slow_window": 4,
            "use_trend_filter": True,
            "trend_ema_window": 8,
        },
    )

    assert ("long", "entry") not in {
        (operation.direction, operation.operation) for operation in operations
    }
    assert result.signal == "WAIT"
    assert result.signal_class == "hold"
    assert "trend_ema" in result.metrics


def test_macd_strategy_returns_long_hold_when_macd_stays_above_signal():
    history = pd.DataFrame(
        {"close": [10, 10, 10, 11, 12, 13, 14, 15]},
        index=pd.date_range("2024-01-01", periods=8),
    )

    result = MACDTrendFollowingStrategy().evaluate(
        history,
        {
            "fast_window": 2,
            "slow_window": 4,
            "signal_window": 2,
            "use_trend_filter": False,
        },
    )

    assert result.signal == "LONG HOLD"
    assert result.direction == "long"
    assert result.signal_class == "long"
    assert "macd" in result.metrics
    assert "signal" in result.metrics
    assert "histogram" in result.metrics


def test_macd_strategy_operation_history_reverses_on_signal_crosses():
    history = pd.DataFrame(
        {"close": [14, 13, 12, 11, 10, 11, 13, 15, 14, 12, 10, 8]},
        index=pd.date_range("2024-01-01", periods=12),
    )

    operations = MACDTrendFollowingStrategy().operation_history(
        history,
        {
            "fast_window": 2,
            "slow_window": 4,
            "signal_window": 2,
            "use_trend_filter": False,
        },
    )

    assert [(operation.direction, operation.operation) for operation in operations] == [
        ("short", "entry"),
        ("short", "exit"),
        ("long", "entry"),
        ("long", "exit"),
        ("short", "entry"),
    ]
    assert all(operation.trade_date for operation in operations)
    assert all(operation.position_size > 0 for operation in operations)


def test_macd_trend_filter_blocks_counter_trend_long_entry():
    history = pd.DataFrame(
        {"close": [100, 100, 100, 10, 11, 12, 13, 14]},
        index=pd.date_range("2024-01-01", periods=8),
    )

    operations = MACDTrendFollowingStrategy().operation_history(
        history,
        {
            "fast_window": 2,
            "slow_window": 4,
            "signal_window": 2,
            "use_trend_filter": True,
            "trend_ema_window": 8,
        },
    )

    assert ("long", "entry") not in {
        (operation.direction, operation.operation) for operation in operations
    }


def test_turtle_strategy_can_add_to_long_breakout_position():
    history = pd.DataFrame(
        {
            "close": [10, 11, 12, 13, 14, 18],
            "high": [10, 11, 12, 13, 14, 17],
            "low": [9, 10, 11, 12, 13, 16],
        },
        index=pd.date_range("2024-01-01", periods=6),
    )

    result = TurtleBreakoutStrategy().evaluate(
        history,
        {"entry_window": 3, "exit_window": 2, "max_units": 4},
    )

    assert result.signal == "LONG ADD"
    assert result.direction == "long"
    assert result.operation == "add_position"
    assert result.metrics["atr"] > 0
    assert "close" not in result.metrics

    operations = TurtleBreakoutStrategy().operation_history(
        history,
        {"entry_window": 3, "exit_window": 2, "max_units": 4},
    )

    assert [(operation.direction, operation.operation) for operation in operations] == [
        ("long", "entry"),
        ("long", "add_position"),
        ("long", "add_position"),
    ]
    assert operations[-1].operation_label == "ADD"
    assert [operation.position_size for operation in operations] == [100.0, 100.0, 100.0]
    assert [operation.position_notional for operation in operations] == [
        1200.0,
        1250.0,
        1300.0,
    ]
    assert [operation.signal_price for operation in operations] == [12.0, 12.5, 13.0]
    assert [operation.metrics["atr"] for operation in operations] == [1.0, 1.0, 1.0]
    assert abs(
        (operations[1].signal_price - operations[0].signal_price)
        - operations[0].metrics["atr"] * 0.5
    ) < 0.0001
    assert abs(
        (operations[2].signal_price - operations[1].signal_price)
        - operations[0].metrics["atr"] * 0.5
    ) < 0.0001


def test_turtle_position_size_is_capped_by_maximum_unit_allocation():
    history = pd.DataFrame(
        {
            "close": [10.00, 10.05, 10.10, 10.20],
            "high": [10.00, 10.05, 10.10, 10.20],
            "low": [9.95, 10.00, 10.05, 10.15],
        },
        index=pd.date_range("2024-01-01", periods=4),
    )

    operations = TurtleBreakoutStrategy().operation_history(
        history,
        {
            "entry_window": 2,
            "exit_window": 1,
            "atr_window": 1,
            "max_units": 4,
        },
    )

    assert [(operation.direction, operation.operation) for operation in operations] == [
        ("long", "entry"),
        ("long", "add_position"),
    ]
    assert [operation.metrics["atr"] for operation in operations] == [0.05, 0.05]
    assert [operation.position_notional for operation in operations] == [2500.0, 2500.0]
    assert [operation.position_size for operation in operations] == [
        248.75621891,
        248.13895782,
    ]


def test_turtle_strategy_uses_normal_channel_for_full_position_exit():
    history = pd.DataFrame(
        {
            "close": [10, 11, 12, 13, 14, 18, 11],
            "high": [10, 11, 12, 13, 14, 17, 12],
            "low": [9, 10, 11, 12, 13, 16, 10],
        },
        index=pd.date_range("2024-01-01", periods=7),
    )

    operations = TurtleBreakoutStrategy().operation_history(
        history,
        {
            "entry_window": 3,
            "exit_window": 2,
            "max_units": 4,
            "exit_atr_ratio": 1.5,
        },
    )

    assert [(operation.direction, operation.operation) for operation in operations] == [
        ("long", "entry"),
        ("long", "add_position"),
        ("long", "add_position"),
        ("long", "exit"),
    ]
    assert "cut_position" not in {operation.operation for operation in operations}
    assert operations[-1].operation_label == "EXIT"
    assert operations[-1].label == "LONG EXIT"
    assert operations[-1].price == 11.0
    assert operations[-1].signal_price == 13.0
    assert operations[-1].metrics["exit_atr_ratio"] == 1.5
    assert operations[-1].metrics["exit_anchor"] == 13.0
    assert operations[-1].metrics["stop_loss"] == 11.5
    assert operations[-1].metrics["normal_exit"] == 13.0
    assert operations[-1].metrics["exit_price"] == 13.0
    assert operations[-1].position_size == 300.0
    assert operations[-1].realized_pnl == 150.0
    assert operations[-1].balance_after == 10150.0


def test_turtle_strategy_uses_last_signal_atr_stop_loss_when_closer_than_channel():
    history = pd.DataFrame(
        {
            "close": [10, 11, 12, 13, 10],
            "high": [10, 11, 12, 13, 11],
            "low": [9, 10, 9, 12, 9],
        },
        index=pd.date_range("2024-01-01", periods=5),
    )

    operations = TurtleBreakoutStrategy().operation_history(
        history,
        {
            "entry_window": 3,
            "exit_window": 2,
            "atr_window": 1,
            "max_units": 4,
            "exit_atr_ratio": 1.5,
        },
    )

    assert [(operation.direction, operation.operation) for operation in operations] == [
        ("long", "entry"),
        ("long", "exit"),
    ]
    assert operations[-1].operation_label == "STOP"
    assert operations[-1].label == "LONG STOP"
    assert operations[-1].signal_price == 10.5
    assert operations[-1].metrics["exit_anchor"] == 12.0
    assert operations[-1].metrics["stop_loss"] == 10.5
    assert operations[-1].metrics["normal_exit"] == 9.0
    assert operations[-1].metrics["exit_price"] == 10.5
    assert operations[-1].realized_pnl == -150.0
    assert operations[-1].balance_after == 9850.0


def test_turtle_moving_average_filter_blocks_counter_trend_long_entry():
    history = pd.DataFrame(
        {
            "close": [100, 100, 100, 10, 11, 12, 13],
            "high": [100, 100, 100, 10, 11, 12, 13],
            "low": [0, 0, 0, 0, 0, 0, 0],
        },
        index=pd.date_range("2024-01-01", periods=7),
    )
    base_params = {"entry_window": 3, "exit_window": 2, "max_units": 4}

    operations_without_filter = TurtleBreakoutStrategy().operation_history(
        history,
        {**base_params, "use_ma_filter": False, "ma_window": 7},
    )
    operations_with_filter = TurtleBreakoutStrategy().operation_history(
        history,
        {**base_params, "use_ma_filter": True, "ma_window": 7},
    )

    assert [(operation.direction, operation.operation) for operation in operations_without_filter] == [
        ("long", "entry"),
    ]
    assert operations_with_filter == []
