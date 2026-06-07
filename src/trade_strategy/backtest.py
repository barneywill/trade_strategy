from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from .strategies import (
    ADD_POSITION,
    ENTRY,
    EXIT,
    INITIAL_BALANCE,
    LONG,
    SHORT,
    StrategyOperation,
    TradeDirection,
)


@dataclass(frozen=True)
class YearlyPerformance:
    year: int
    strategy_start: float
    strategy_end: float
    strategy_return_pct: float
    buy_hold_start: float
    buy_hold_end: float
    buy_hold_return_pct: float
    difference_pct: float


@dataclass(frozen=True)
class BacktestResult:
    yearly: list[YearlyPerformance]
    strategy_final: float
    buy_hold_final: float
    strategy_total_return_pct: float
    buy_hold_total_return_pct: float


def yearly_backtest(
    history: pd.DataFrame,
    operations: list[StrategyOperation],
) -> BacktestResult:
    frame = history.dropna(subset=["close"]).copy()
    if frame.empty:
        return BacktestResult([], INITIAL_BALANCE, INITIAL_BALANCE, 0.0, 0.0)

    equity = _equity_curve(frame, operations)
    yearly = []
    for year, group in equity.groupby(equity.index.year):
        first = group.iloc[0]
        last = group.iloc[-1]
        strategy_return = _return_pct(first["strategy"], last["strategy"])
        buy_hold_return = _return_pct(first["buy_hold"], last["buy_hold"])
        yearly.append(
            YearlyPerformance(
                year=int(year),
                strategy_start=round(float(first["strategy"]), 2),
                strategy_end=round(float(last["strategy"]), 2),
                strategy_return_pct=strategy_return,
                buy_hold_start=round(float(first["buy_hold"]), 2),
                buy_hold_end=round(float(last["buy_hold"]), 2),
                buy_hold_return_pct=buy_hold_return,
                difference_pct=round(strategy_return - buy_hold_return, 2),
            )
        )

    strategy_final = float(equity["strategy"].iloc[-1])
    buy_hold_final = float(equity["buy_hold"].iloc[-1])
    return BacktestResult(
        yearly=yearly,
        strategy_final=round(strategy_final, 2),
        buy_hold_final=round(buy_hold_final, 2),
        strategy_total_return_pct=_return_pct(INITIAL_BALANCE, strategy_final),
        buy_hold_total_return_pct=_return_pct(INITIAL_BALANCE, buy_hold_final),
    )


def _equity_curve(
    history: pd.DataFrame,
    operations: list[StrategyOperation],
) -> pd.DataFrame:
    operations_by_date: dict[date, list[StrategyOperation]] = {}
    for operation in operations:
        operations_by_date.setdefault(date.fromisoformat(operation.trade_date), []).append(
            operation
        )

    balance = INITIAL_BALANCE
    open_direction: TradeDirection | None = None
    lots: list[tuple[float, float]] = []
    buy_hold_quantity = INITIAL_BALANCE / float(history["close"].iloc[0])
    rows = []

    for trade_date, row in history.iterrows():
        close = float(row["close"])
        for operation in operations_by_date.get(trade_date.date(), []):
            if operation.operation == ENTRY:
                open_direction = operation.direction
                lots = [(operation.signal_price, operation.position_size)]
            elif operation.operation == ADD_POSITION:
                if open_direction != operation.direction:
                    open_direction = operation.direction
                    lots = []
                lots.append((operation.signal_price, operation.position_size))
            elif operation.operation == EXIT:
                balance = operation.balance_after
                open_direction = None
                lots = []

        strategy_equity = balance + _unrealized_pnl(open_direction, close, lots)
        rows.append(
            {
                "trade_date": trade_date,
                "strategy": strategy_equity,
                "buy_hold": buy_hold_quantity * close,
            }
        )

    return pd.DataFrame(rows).set_index("trade_date")


def _unrealized_pnl(
    direction: TradeDirection | None,
    close: float,
    lots: list[tuple[float, float]],
) -> float:
    if direction == LONG:
        return sum((close - entry_price) * quantity for entry_price, quantity in lots)
    if direction == SHORT:
        return sum((entry_price - close) * quantity for entry_price, quantity in lots)
    return 0.0


def _return_pct(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return float(round(((float(end) / float(start)) - 1) * 100, 2))
