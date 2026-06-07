import pandas as pd

from trade_strategy.charting import build_operation_chart
from trade_strategy.strategies import StrategyOperation, TurtleBreakoutStrategy


def test_operation_chart_includes_close_line_and_directional_dots():
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
        {"entry_window": 3, "exit_window": 2, "max_units": 4},
    )

    chart = build_operation_chart(history, operations)

    assert chart["has_data"]
    assert chart["line_points"]
    assert {dot["class"] for dot in chart["dots"]} == {"long", "long exit"}
    assert chart["dots"][-1]["class"] == "long exit"
    assert len(chart["dots"]) == len(operations)


def test_operation_chart_extends_to_open_cycle_started_before_last_year():
    history = pd.DataFrame(
        {"close": range(1, 391)},
        index=pd.date_range("2025-05-12", periods=390),
    )
    operations = [
        StrategyOperation(
            trade_date="2025-05-12",
            direction="long",
            operation="entry",
            price=10.0,
            signal_price=10.0,
            detail="Entry",
            metrics={},
            signal_class="long",
        ),
        StrategyOperation(
            trade_date="2025-05-15",
            direction="long",
            operation="add_position",
            price=11.0,
            signal_price=11.0,
            detail="Add",
            metrics={},
            signal_class="long",
        ),
    ]

    chart = build_operation_chart(history, operations, days=365)

    assert chart["start_date"] == "2025-05-12"
    assert len(chart["dots"]) == 2


def test_operation_chart_includes_moving_average_points_when_requested():
    history = pd.DataFrame(
        {"close": [10, 11, 12, 13, 14]},
        index=pd.date_range("2024-01-01", periods=5),
    )

    chart = build_operation_chart(
        history,
        [],
        moving_average_window=3,
    )

    assert chart["moving_average_window"] == 3
    assert chart["moving_average_points"]
    assert len(chart["moving_average_points"].split()) == 3


def test_operation_chart_includes_ema_lines_when_requested():
    history = pd.DataFrame(
        {"close": [10, 11, 12, 13, 14]},
        index=pd.date_range("2024-01-01", periods=5),
    )

    chart = build_operation_chart(
        history,
        [],
        ema_windows={"fast": 2, "slow": 4},
    )

    assert [line["label"] for line in chart["ema_lines"]] == [
        "Fast EMA 2",
        "Slow EMA 4",
    ]
    assert [line["class"] for line in chart["ema_lines"]] == [
        "fast-ema-line",
        "slow-ema-line",
    ]
    assert all(line["points"] for line in chart["ema_lines"])
    assert all(len(line["points"].split()) == 5 for line in chart["ema_lines"])
