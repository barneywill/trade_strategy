from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from .strategies import ENTRY, EXIT, StrategyOperation


def build_operation_chart(
    history: pd.DataFrame,
    operations: list[StrategyOperation],
    days: int = 365,
    moving_average_window: int | None = None,
    ema_windows: dict[str, int] | None = None,
) -> dict[str, Any]:
    frame = history.dropna(subset=["close"]).copy()
    if frame.empty:
        return {"has_data": False}

    if moving_average_window is not None:
        frame["moving_average"] = (
            frame["close"].rolling(int(moving_average_window)).mean()
        )
    if ema_windows:
        for name, window in ema_windows.items():
            frame[f"{name}_ema"] = frame["close"].ewm(
                span=int(window),
                adjust=False,
            ).mean()

    end = frame.index.max()
    start = end - pd.Timedelta(days=days)
    active_cycle_start = _active_cycle_start(operations, end.date())
    if active_cycle_start is not None and active_cycle_start < start.date():
        start = pd.Timestamp(active_cycle_start)

    frame = frame[frame.index >= start]
    if frame.empty:
        return {"has_data": False}

    operation_points = [
        operation
        for operation in operations
        if start.date() <= date.fromisoformat(operation.trade_date) <= end.date()
    ]
    prices = [float(value) for value in frame["close"]]
    prices.extend(float(operation.signal_price) for operation in operation_points)
    moving_average_points = []
    if "moving_average" in frame:
        moving_average_points = [
            (index, float(value))
            for index, value in frame["moving_average"].dropna().items()
        ]
        prices.extend(value for _, value in moving_average_points)
    ema_lines = []
    if ema_windows:
        for name, window in ema_windows.items():
            column_name = f"{name}_ema"
            points = [
                (index, float(value))
                for index, value in frame[column_name].dropna().items()
            ]
            prices.extend(value for _, value in points)
            ema_lines.append(
                {
                    "class": f"{name}-ema-line",
                    "label": f"{name.title()} EMA {int(window)}",
                    "points": points,
                }
            )

    min_price = min(prices)
    max_price = max(prices)
    if min_price == max_price:
        min_price -= 1
        max_price += 1

    width = 1000
    height = 280
    padding = {"left": 54, "right": 18, "top": 20, "bottom": 34}
    start_ordinal = frame.index.min().date().toordinal()
    end_ordinal = frame.index.max().date().toordinal()

    close_points = [
        f"{_x_for_date(index.date(), start_ordinal, end_ordinal, width, padding):.2f},"
        f"{_y_for_price(float(row['close']), min_price, max_price, height, padding):.2f}"
        for index, row in frame.iterrows()
    ]
    moving_average_line_points = [
        f"{_x_for_date(index.date(), start_ordinal, end_ordinal, width, padding):.2f},"
        f"{_y_for_price(value, min_price, max_price, height, padding):.2f}"
        for index, value in moving_average_points
    ]
    for line in ema_lines:
        line["points"] = " ".join(
            f"{_x_for_date(index.date(), start_ordinal, end_ordinal, width, padding):.2f},"
            f"{_y_for_price(value, min_price, max_price, height, padding):.2f}"
            for index, value in line["points"]
        )

    dots = [
        {
            "x": round(
                _x_for_date(
                    date.fromisoformat(operation.trade_date),
                    start_ordinal,
                    end_ordinal,
                    width,
                    padding,
                ),
                2,
            ),
            "y": round(
                _y_for_price(
                    float(operation.signal_price),
                    min_price,
                    max_price,
                    height,
                    padding,
                ),
                2,
            ),
            "class": _dot_class(operation),
            "label": f"{operation.trade_date} {operation.label} @ {operation.signal_price:.4f}",
        }
        for operation in operation_points
    ]

    return {
        "has_data": True,
        "width": width,
        "height": height,
        "line_points": " ".join(close_points),
        "moving_average_points": " ".join(moving_average_line_points),
        "moving_average_window": moving_average_window,
        "ema_lines": ema_lines,
        "dots": dots,
        "min_price": round(min_price, 4),
        "max_price": round(max_price, 4),
        "start_date": frame.index.min().date().isoformat(),
        "end_date": frame.index.max().date().isoformat(),
    }


def _dot_class(operation: StrategyOperation) -> str:
    if operation.operation == EXIT:
        return f"{operation.direction} exit"
    return operation.direction


def _active_cycle_start(
    operations: list[StrategyOperation],
    end: date,
) -> date | None:
    direction = None
    cycle_start = None

    for operation in operations:
        operation_date = date.fromisoformat(operation.trade_date)
        if operation_date > end:
            continue

        if operation.operation == ENTRY:
            direction = operation.direction
            cycle_start = operation_date
        elif operation.operation == EXIT and direction == operation.direction:
            direction = None
            cycle_start = None

    return cycle_start


def _x_for_date(
    value: date,
    start_ordinal: int,
    end_ordinal: int,
    width: int,
    padding: dict[str, int],
) -> float:
    plot_width = width - padding["left"] - padding["right"]
    if start_ordinal == end_ordinal:
        return padding["left"] + plot_width / 2
    return padding["left"] + (
        (value.toordinal() - start_ordinal) / (end_ordinal - start_ordinal)
    ) * plot_width


def _y_for_price(
    value: float,
    min_price: float,
    max_price: float,
    height: int,
    padding: dict[str, int],
) -> float:
    plot_height = height - padding["top"] - padding["bottom"]
    return padding["top"] + ((max_price - value) / (max_price - min_price)) * plot_height
