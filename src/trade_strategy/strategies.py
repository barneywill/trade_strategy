from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any

import pandas as pd


TradeDirection = str
OperationType = str

LONG = "long"
SHORT = "short"
ENTRY = "entry"
ADD_POSITION = "add_position"
EXIT = "exit"
INITIAL_BALANCE = 10_000.0
POSITION_RISK_PCT = 0.01


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    label: str
    default: int | float | str | bool
    kind: str = "number"
    minimum: int | float | None = None
    maximum: int | float | None = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyResult:
    signal: str
    detail: str
    metrics: dict[str, float | str]
    signal_class: str | None = None
    direction: TradeDirection | None = None
    operation: OperationType | None = None


@dataclass(frozen=True)
class StrategyOperation:
    trade_date: str
    direction: TradeDirection
    operation: OperationType
    price: float
    signal_price: float
    detail: str
    metrics: dict[str, float | str]
    signal_class: str
    position_size: float = 0.0
    position_notional: float = 0.0
    realized_pnl: float = 0.0
    balance_after: float = INITIAL_BALANCE

    @property
    def label(self) -> str:
        return f"{self.direction.upper()} {self.operation_label}"

    @property
    def operation_label(self) -> str:
        if self.operation == EXIT and self.detail.startswith("Stop loss exit"):
            return "STOP"
        return operation_label(self.operation)


class TradeStrategy(ABC):
    name: str
    label: str
    description: str
    parameters: tuple[ParameterSpec, ...]

    @property
    def default_params(self) -> dict[str, Any]:
        return {parameter.name: parameter.default for parameter in self.parameters}

    @abstractmethod
    def evaluate(self, history: pd.DataFrame, params: dict[str, Any]) -> StrategyResult:
        """Return the current trading status for the given OHLCV history."""

    def operation_history(
        self, history: pd.DataFrame, params: dict[str, Any]
    ) -> list[StrategyOperation]:
        frame = history.dropna(subset=["close"]).copy()
        operations = []

        for index in range(1, len(frame) + 1):
            window = frame.iloc[:index]
            result = self.evaluate(window, params)
            if result.direction is None or result.operation is None:
                continue

            operations.append(_operation_from_result(window, result))

        return _apply_position_sizing(operations, self.position_unit_count(params))

    def enough_data(self, history: pd.DataFrame, required_rows: int) -> bool:
        return len(history.dropna(subset=["close"])) >= required_rows

    def position_unit_count(self, params: dict[str, Any]) -> int:
        return 1


class EMACrossoverStrategy(TradeStrategy):
    name = "ema_crossover"
    label = "EMA Crossover"
    description = "Compares a fast and slow exponential moving average with an optional trend EMA filter."
    parameters = (
        ParameterSpec("fast_window", "Fast EMA window", 12, minimum=2, maximum=200),
        ParameterSpec("slow_window", "Slow EMA window", 26, minimum=3, maximum=400),
        ParameterSpec("use_trend_filter", "Trend EMA filter", False, kind="checkbox"),
        ParameterSpec("trend_ema_window", "Trend EMA window", 200, minimum=20, maximum=500),
    )

    def evaluate(self, history: pd.DataFrame, params: dict[str, Any]) -> StrategyResult:
        frame = history.dropna(subset=["close"]).copy()
        values = _ema_values(frame, params)
        if values is None:
            return StrategyResult("WAIT", "Not enough history.", {}, "hold")

        latest = values.iloc[-1]
        previous = values.iloc[-2]
        close = float(frame["close"].iloc[-1])
        use_trend_filter = bool(params.get("use_trend_filter", False))
        metrics = _ema_metrics(latest, use_trend_filter)
        latest_fast = float(latest["fast_ema"])
        latest_slow = float(latest["slow_ema"])
        previous_fast = float(previous["fast_ema"])
        previous_slow = float(previous["slow_ema"])
        long_allowed = _passes_ema_trend_filter(
            close,
            latest,
            LONG,
            use_trend_filter,
        )
        short_allowed = _passes_ema_trend_filter(
            close,
            latest,
            SHORT,
            use_trend_filter,
        )

        if previous_fast <= previous_slow and latest_fast > latest_slow and long_allowed:
            return StrategyResult(
                "LONG ENTRY",
                "Fast EMA crossed above slow EMA.",
                metrics,
                LONG,
                LONG,
                ENTRY,
            )
        if previous_fast >= previous_slow and latest_fast < latest_slow and short_allowed:
            return StrategyResult(
                "SHORT ENTRY",
                "Fast EMA crossed below slow EMA.",
                metrics,
                SHORT,
                SHORT,
                ENTRY,
            )
        if latest_fast > latest_slow:
            if not long_allowed:
                return StrategyResult(
                    "WAIT",
                    "EMA trend filter blocks the long direction.",
                    metrics,
                    "hold",
                )
            return StrategyResult(
                "LONG HOLD",
                "Holding long EMA direction.",
                metrics,
                LONG,
                LONG,
            )
        if latest_fast < latest_slow:
            if not short_allowed:
                return StrategyResult(
                    "WAIT",
                    "EMA trend filter blocks the short direction.",
                    metrics,
                    "hold",
                )
            return StrategyResult(
                "SHORT HOLD",
                "Holding short EMA direction.",
                metrics,
                SHORT,
                SHORT,
            )

        return StrategyResult(
            "WAIT",
            "Fast and slow EMA are nearly equal.",
            metrics,
            "hold",
        )

    def operation_history(
        self, history: pd.DataFrame, params: dict[str, Any]
    ) -> list[StrategyOperation]:
        frame = history.dropna(subset=["close"]).copy()
        values = _ema_values(frame, params)
        if values is None:
            return []

        use_trend_filter = bool(params.get("use_trend_filter", False))
        operations: list[StrategyOperation] = []
        current_direction: TradeDirection | None = None

        slow_window = int(params.get("slow_window", 26))
        for index in range(slow_window + 1, len(values)):
            trade_date = values.index[index]
            previous = values.iloc[index - 1]
            latest = values.iloc[index]
            close = float(frame.loc[trade_date, "close"])
            metrics = _ema_metrics(latest, use_trend_filter)
            crossed_long = (
                float(previous["fast_ema"]) <= float(previous["slow_ema"])
                and float(latest["fast_ema"]) > float(latest["slow_ema"])
            )
            crossed_short = (
                float(previous["fast_ema"]) >= float(previous["slow_ema"])
                and float(latest["fast_ema"]) < float(latest["slow_ema"])
            )
            long_direction = float(latest["fast_ema"]) > float(latest["slow_ema"])
            short_direction = float(latest["fast_ema"]) < float(latest["slow_ema"])
            long_allowed = _passes_ema_trend_filter(
                close,
                latest,
                LONG,
                use_trend_filter,
            )
            short_allowed = _passes_ema_trend_filter(
                close,
                latest,
                SHORT,
                use_trend_filter,
            )

            if current_direction == LONG:
                if crossed_short or not long_allowed:
                    operations.append(
                        _make_operation(
                            trade_date,
                            LONG,
                            EXIT,
                            close,
                            close,
                            "EMA direction no longer supports the long position.",
                            metrics,
                        )
                    )
                    current_direction = None
                if crossed_short and short_allowed:
                    operations.append(
                        _make_operation(
                            trade_date,
                            SHORT,
                            ENTRY,
                            close,
                            close,
                            "Fast EMA crossed below slow EMA.",
                            metrics,
                        )
                    )
                    current_direction = SHORT
                continue

            if current_direction == SHORT:
                if crossed_long or not short_allowed:
                    operations.append(
                        _make_operation(
                            trade_date,
                            SHORT,
                            EXIT,
                            close,
                            close,
                            "EMA direction no longer supports the short position.",
                            metrics,
                        )
                    )
                    current_direction = None
                if crossed_long and long_allowed:
                    operations.append(
                        _make_operation(
                            trade_date,
                            LONG,
                            ENTRY,
                            close,
                            close,
                            "Fast EMA crossed above slow EMA.",
                            metrics,
                        )
                    )
                    current_direction = LONG
                continue

            if long_direction and long_allowed:
                operations.append(
                    _make_operation(
                        trade_date,
                        LONG,
                        ENTRY,
                        close,
                        close,
                        (
                            "Fast EMA crossed above slow EMA."
                            if crossed_long
                            else "Initial long EMA direction."
                        ),
                        metrics,
                    )
                )
                current_direction = LONG
            elif short_direction and short_allowed:
                operations.append(
                    _make_operation(
                        trade_date,
                        SHORT,
                        ENTRY,
                        close,
                        close,
                        (
                            "Fast EMA crossed below slow EMA."
                            if crossed_short
                            else "Initial short EMA direction."
                        ),
                        metrics,
                    )
                )
                current_direction = SHORT

        return _apply_position_sizing(operations, self.position_unit_count(params))


def _ema_values(
    frame: pd.DataFrame,
    params: dict[str, Any],
) -> pd.DataFrame | None:
    fast_window = int(params.get("fast_window", 12))
    slow_window = int(params.get("slow_window", 26))
    trend_ema_window = int(params.get("trend_ema_window", 200))

    if fast_window >= slow_window:
        return None
    if len(frame) < slow_window + 2:
        return None

    close = frame["close"].dropna().astype(float)
    return pd.DataFrame(
        {
            "fast_ema": close.ewm(span=fast_window, adjust=False).mean(),
            "slow_ema": close.ewm(span=slow_window, adjust=False).mean(),
            "trend_ema": close.ewm(span=trend_ema_window, adjust=False).mean(),
        }
    )


def _ema_metrics(row, use_trend_filter: bool) -> dict[str, float]:
    metrics = {
        "fast_ema": round(float(row["fast_ema"]), 4),
        "slow_ema": round(float(row["slow_ema"]), 4),
    }
    if use_trend_filter:
        metrics["trend_ema"] = round(float(row["trend_ema"]), 4)
    return metrics


def _passes_ema_trend_filter(
    close: float,
    row,
    direction: TradeDirection,
    use_trend_filter: bool,
) -> bool:
    if not use_trend_filter:
        return True
    trend_ema = row.get("trend_ema")
    if trend_ema is None or pd.isna(trend_ema):
        return False
    if direction == LONG:
        return close > float(trend_ema)
    return close < float(trend_ema)


class MACDTrendFollowingStrategy(TradeStrategy):
    name = "macd_trend_following"
    label = "MACD Trend"
    description = "Uses MACD signal-line crosses with an optional long-term EMA trend filter."
    parameters = (
        ParameterSpec("fast_window", "Fast EMA window", 12, minimum=2, maximum=200),
        ParameterSpec("slow_window", "Slow EMA window", 26, minimum=3, maximum=400),
        ParameterSpec("signal_window", "Signal EMA window", 9, minimum=2, maximum=100),
        ParameterSpec("use_trend_filter", "Trend EMA filter", True, kind="checkbox"),
        ParameterSpec("trend_ema_window", "Trend EMA window", 200, minimum=20, maximum=500),
    )

    def evaluate(self, history: pd.DataFrame, params: dict[str, Any]) -> StrategyResult:
        operations = self.operation_history(history, params)
        frame = history.dropna(subset=["close"]).copy()
        if frame.empty:
            return StrategyResult("WAIT", "Not enough history.", {}, "hold")
        if operations and operations[-1].trade_date == frame.index[-1].date().isoformat():
            latest = operations[-1]
            return StrategyResult(
                latest.label,
                latest.detail,
                latest.metrics,
                latest.signal_class,
                latest.direction,
                latest.operation,
            )

        macd = _macd_values(frame, params)
        if macd is None:
            return StrategyResult("WAIT", "Not enough history.", {}, "hold")

        latest = macd.iloc[-1]
        close = float(frame["close"].iloc[-1])
        metrics = _macd_metrics(latest, bool(params.get("use_trend_filter", True)))
        direction = _current_direction_from_operations(operations)

        if direction == LONG:
            return StrategyResult(
                "LONG HOLD",
                "Holding long MACD trend direction.",
                metrics,
                LONG,
                LONG,
            )
        if direction == SHORT:
            return StrategyResult(
                "SHORT HOLD",
                "Holding short MACD trend direction.",
                metrics,
                SHORT,
                SHORT,
            )
        if float(latest["macd"]) > float(latest["signal"]) and _passes_macd_trend_filter(
            close,
            latest,
            LONG,
            bool(params.get("use_trend_filter", True)),
        ):
            return StrategyResult(
                "WAIT",
                "MACD is above signal, waiting for a fresh long cross.",
                metrics,
                "hold",
            )
        if float(latest["macd"]) < float(latest["signal"]) and _passes_macd_trend_filter(
            close,
            latest,
            SHORT,
            bool(params.get("use_trend_filter", True)),
        ):
            return StrategyResult(
                "WAIT",
                "MACD is below signal, waiting for a fresh short cross.",
                metrics,
                "hold",
            )
        return StrategyResult(
            "WAIT",
            "MACD trend filter is not aligned.",
            metrics,
            "hold",
        )

    def operation_history(
        self, history: pd.DataFrame, params: dict[str, Any]
    ) -> list[StrategyOperation]:
        frame = history.dropna(subset=["close"]).copy()
        macd = _macd_values(frame, params)
        if macd is None:
            return []

        use_trend_filter = bool(params.get("use_trend_filter", True))
        operations: list[StrategyOperation] = []
        current_direction: TradeDirection | None = None

        for index in range(1, len(macd)):
            trade_date = macd.index[index]
            previous = macd.iloc[index - 1]
            latest = macd.iloc[index]
            close = float(frame.loc[trade_date, "close"])
            metrics = _macd_metrics(latest, use_trend_filter)
            crossed_long = (
                float(previous["macd"]) <= float(previous["signal"])
                and float(latest["macd"]) > float(latest["signal"])
            )
            crossed_short = (
                float(previous["macd"]) >= float(previous["signal"])
                and float(latest["macd"]) < float(latest["signal"])
            )
            long_allowed = _passes_macd_trend_filter(
                close,
                latest,
                LONG,
                use_trend_filter,
            )
            short_allowed = _passes_macd_trend_filter(
                close,
                latest,
                SHORT,
                use_trend_filter,
            )
            operation: StrategyOperation | None = None

            if current_direction == LONG:
                if crossed_short or not long_allowed:
                    operation = _make_operation(
                        trade_date,
                        LONG,
                        EXIT,
                        close,
                        close,
                        "MACD trend no longer supports the long position.",
                        metrics,
                    )
                    current_direction = None
                    operations.append(operation)
                if crossed_short and short_allowed:
                    operations.append(
                        _make_operation(
                            trade_date,
                            SHORT,
                            ENTRY,
                            close,
                            close,
                            "MACD crossed below signal while price was below the trend EMA.",
                            metrics,
                        )
                    )
                    current_direction = SHORT
                continue

            if current_direction == SHORT:
                if crossed_long or not short_allowed:
                    operation = _make_operation(
                        trade_date,
                        SHORT,
                        EXIT,
                        close,
                        close,
                        "MACD trend no longer supports the short position.",
                        metrics,
                    )
                    current_direction = None
                    operations.append(operation)
                if crossed_long and long_allowed:
                    operations.append(
                        _make_operation(
                            trade_date,
                            LONG,
                            ENTRY,
                            close,
                            close,
                            "MACD crossed above signal while price was above the trend EMA.",
                            metrics,
                        )
                    )
                    current_direction = LONG
                continue

            if crossed_long and long_allowed:
                operation = _make_operation(
                    trade_date,
                    LONG,
                    ENTRY,
                    close,
                    close,
                    "MACD crossed above signal while price was above the trend EMA.",
                    metrics,
                )
                current_direction = LONG
            elif crossed_short and short_allowed:
                operation = _make_operation(
                    trade_date,
                    SHORT,
                    ENTRY,
                    close,
                    close,
                    "MACD crossed below signal while price was below the trend EMA.",
                    metrics,
                )
                current_direction = SHORT

            if operation is not None:
                operations.append(operation)

        return _apply_position_sizing(operations, self.position_unit_count(params))


class TurtleBreakoutStrategy(TradeStrategy):
    name = "turtle_breakout"
    label = "Turtle Breakout"
    description = "Uses Donchian-style highs and lows for dual-direction breakout signals."
    parameters = (
        ParameterSpec("entry_window", "Entry breakout window", 20, minimum=5, maximum=260),
        ParameterSpec("exit_window", "Exit breakout window", 10, minimum=2, maximum=120),
        ParameterSpec("atr_window", "ATR window", 20, minimum=2, maximum=120),
        ParameterSpec("exit_atr_ratio", "Exit ATR ratio", 2.0, minimum=0.1, maximum=10.0),
        ParameterSpec("use_ma_filter", "Moving Average filter", False, kind="checkbox"),
        ParameterSpec("ma_window", "MA filter window", 200, minimum=5, maximum=400),
        ParameterSpec("max_units", "Maximum position units", 4, minimum=1, maximum=10),
    )

    def evaluate(self, history: pd.DataFrame, params: dict[str, Any]) -> StrategyResult:
        operations = self.operation_history(history, params)
        frame = history.dropna(subset=["close", "high", "low"]).copy()
        if operations and operations[-1].trade_date == frame.index[-1].date().isoformat():
            latest = operations[-1]
            return StrategyResult(
                latest.label,
                latest.detail,
                latest.metrics,
                latest.signal_class,
                latest.direction,
                latest.operation,
            )

        levels = _turtle_levels(frame, params)
        if levels is None:
            return StrategyResult("WAIT", "Not enough history.", {}, "hold")

        close = float(frame["close"].iloc[-1])
        metrics = _latest_turtle_metrics(
            close,
            levels,
            bool(params.get("use_ma_filter", False)),
        )
        direction = _current_direction_from_operations(operations)

        if direction == LONG:
            return StrategyResult(
                "LONG HOLD",
                "Holding long Turtle direction.",
                metrics,
                LONG,
                LONG,
            )
        if direction == SHORT:
            return StrategyResult(
                "SHORT HOLD",
                "Holding short Turtle direction.",
                metrics,
                SHORT,
                SHORT,
            )

        return StrategyResult(
            "WAIT",
            "Price has not opened a Turtle position.",
            metrics,
            "hold",
        )

    def operation_history(
        self, history: pd.DataFrame, params: dict[str, Any]
    ) -> list[StrategyOperation]:
        frame = history.dropna(subset=["close", "high", "low"]).copy()
        levels = _turtle_levels(frame, params)
        if levels is None:
            return []

        max_units = max(1, int(params.get("max_units", 4)))
        exit_atr_ratio = float(params.get("exit_atr_ratio", 2.0))
        use_ma_filter = bool(params.get("use_ma_filter", False))
        operations: list[StrategyOperation] = []
        direction: TradeDirection | None = None
        units = 0
        last_unit_signal_price: float | None = None
        entry_atr: float | None = None

        for trade_date, row in frame.iterrows():
            row_levels = levels.loc[trade_date]
            required_levels = ["entry_high", "entry_low", "exit_high", "exit_low", "atr"]
            if use_ma_filter:
                required_levels.append("moving_average")
            if row_levels[required_levels].isna().any():
                continue

            close = float(row["close"])
            metrics = _turtle_metrics(close, row_levels, use_ma_filter)
            day_operations: list[StrategyOperation] = []

            if direction is None:
                if close > float(row_levels["entry_high"]) and _passes_ma_filter(
                    close, row_levels, LONG, use_ma_filter
                ):
                    entry_atr = float(row_levels["atr"])
                    entry_price = float(row_levels["entry_high"])
                    day_operations.append(
                        _make_operation(
                            trade_date,
                            LONG,
                            ENTRY,
                            close,
                            entry_price,
                            "Close broke above the long entry channel.",
                            _with_turtle_operation_metrics(
                                metrics,
                                entry_atr,
                                _turtle_stop_loss_price(
                                    entry_price,
                                    entry_atr,
                                    LONG,
                                    exit_atr_ratio,
                                ),
                                float(row_levels["exit_low"]),
                                None,
                                exit_atr_ratio,
                                entry_price,
                            ),
                        )
                    )
                    direction = LONG
                    units = 1
                    last_unit_signal_price = entry_price
                    add_operations, units, last_unit_signal_price = (
                        _turtle_add_operations_for_close(
                            trade_date,
                            LONG,
                            close,
                            row_levels,
                            metrics,
                            entry_atr,
                            last_unit_signal_price,
                            units,
                            max_units,
                            exit_atr_ratio,
                            use_ma_filter,
                        )
                    )
                    day_operations.extend(add_operations)
                elif close < float(row_levels["entry_low"]) and _passes_ma_filter(
                    close, row_levels, SHORT, use_ma_filter
                ):
                    entry_atr = float(row_levels["atr"])
                    entry_price = float(row_levels["entry_low"])
                    day_operations.append(
                        _make_operation(
                            trade_date,
                            SHORT,
                            ENTRY,
                            close,
                            entry_price,
                            "Close broke below the short entry channel.",
                            _with_turtle_operation_metrics(
                                metrics,
                                entry_atr,
                                _turtle_stop_loss_price(
                                    entry_price,
                                    entry_atr,
                                    SHORT,
                                    exit_atr_ratio,
                                ),
                                float(row_levels["exit_high"]),
                                None,
                                exit_atr_ratio,
                                entry_price,
                            ),
                        )
                    )
                    direction = SHORT
                    units = 1
                    last_unit_signal_price = entry_price
                    add_operations, units, last_unit_signal_price = (
                        _turtle_add_operations_for_close(
                            trade_date,
                            SHORT,
                            close,
                            row_levels,
                            metrics,
                            entry_atr,
                            last_unit_signal_price,
                            units,
                            max_units,
                            exit_atr_ratio,
                            use_ma_filter,
                        )
                    )
                    day_operations.extend(add_operations)
            elif direction == LONG:
                exit_levels = _turtle_exit_levels(
                    row_levels,
                    last_unit_signal_price,
                    entry_atr,
                    LONG,
                    exit_atr_ratio,
                )
                if exit_levels is not None and close <= exit_levels["price"]:
                    day_operations.append(
                        _make_operation(
                            trade_date,
                            LONG,
                            EXIT,
                            close,
                            exit_levels["price"],
                            _exit_detail(LONG, exit_levels["kind"], exit_atr_ratio),
                            _with_turtle_operation_metrics(
                                metrics,
                                entry_atr,
                                exit_levels["stop_loss_price"],
                                exit_levels["normal_exit_price"],
                                exit_levels["price"],
                                exit_atr_ratio,
                                exit_levels["anchor_price"],
                            ),
                        )
                    )
                    direction = None
                    units = 0
                    last_unit_signal_price = None
                    entry_atr = None
                else:
                    add_operations, units, last_unit_signal_price = (
                        _turtle_add_operations_for_close(
                            trade_date,
                            LONG,
                            close,
                            row_levels,
                            metrics,
                            entry_atr,
                            last_unit_signal_price,
                            units,
                            max_units,
                            exit_atr_ratio,
                            use_ma_filter,
                        )
                    )
                    day_operations.extend(add_operations)
            elif direction == SHORT:
                exit_levels = _turtle_exit_levels(
                    row_levels,
                    last_unit_signal_price,
                    entry_atr,
                    SHORT,
                    exit_atr_ratio,
                )
                if exit_levels is not None and close >= exit_levels["price"]:
                    day_operations.append(
                        _make_operation(
                            trade_date,
                            SHORT,
                            EXIT,
                            close,
                            exit_levels["price"],
                            _exit_detail(SHORT, exit_levels["kind"], exit_atr_ratio),
                            _with_turtle_operation_metrics(
                                metrics,
                                entry_atr,
                                exit_levels["stop_loss_price"],
                                exit_levels["normal_exit_price"],
                                exit_levels["price"],
                                exit_atr_ratio,
                                exit_levels["anchor_price"],
                            ),
                        )
                    )
                    direction = None
                    units = 0
                    last_unit_signal_price = None
                    entry_atr = None
                else:
                    add_operations, units, last_unit_signal_price = (
                        _turtle_add_operations_for_close(
                            trade_date,
                            SHORT,
                            close,
                            row_levels,
                            metrics,
                            entry_atr,
                            last_unit_signal_price,
                            units,
                            max_units,
                            exit_atr_ratio,
                            use_ma_filter,
                        )
                    )
                    day_operations.extend(add_operations)

            operations.extend(day_operations)

        return _apply_position_sizing(operations, self.position_unit_count(params))

    def position_unit_count(self, params: dict[str, Any]) -> int:
        return max(1, int(params.get("max_units", 4)))


STRATEGIES: dict[str, TradeStrategy] = {
    strategy.name: strategy
    for strategy in (
        EMACrossoverStrategy(),
        MACDTrendFollowingStrategy(),
        TurtleBreakoutStrategy(),
    )
}


def default_strategy_params() -> dict[str, dict[str, Any]]:
    return {name: strategy.default_params for name, strategy in STRATEGIES.items()}


def _operation_from_result(
    history: pd.DataFrame, result: StrategyResult
) -> StrategyOperation:
    if result.direction is None or result.operation is None:
        raise ValueError("Operation results require both direction and operation.")

    return StrategyOperation(
        trade_date=history.index[-1].date().isoformat(),
        direction=result.direction,
        operation=result.operation,
        price=round(float(history["close"].iloc[-1]), 4),
        signal_price=round(float(history["close"].iloc[-1]), 4),
        detail=result.detail,
        metrics=result.metrics,
        signal_class=result.signal_class or result.direction,
    )


def _with_operation(
    result: StrategyResult, operation: OperationType, detail: str
) -> StrategyResult:
    return StrategyResult(
        signal=f"{result.direction.upper()} {operation_label(operation)}",
        detail=detail,
        metrics=result.metrics,
        signal_class=result.signal_class,
        direction=result.direction,
        operation=operation,
    )


def _make_operation(
    trade_date,
    direction: TradeDirection,
    operation: OperationType,
    price: float,
    signal_price: float,
    detail: str,
    metrics: dict[str, float | str],
) -> StrategyOperation:
    return StrategyOperation(
        trade_date=trade_date.date().isoformat(),
        direction=direction,
        operation=operation,
        price=round(price, 4),
        signal_price=round(signal_price, 4),
        detail=detail,
        metrics=metrics,
        signal_class=direction,
    )


def _macd_values(
    frame: pd.DataFrame,
    params: dict[str, Any],
) -> pd.DataFrame | None:
    fast_window = int(params.get("fast_window", 12))
    slow_window = int(params.get("slow_window", 26))
    signal_window = int(params.get("signal_window", 9))
    trend_ema_window = int(params.get("trend_ema_window", 200))

    if fast_window >= slow_window:
        return None
    if len(frame) < max(slow_window, signal_window) + 2:
        return None

    close = frame["close"].dropna().astype(float)
    fast_ema = close.ewm(span=fast_window, adjust=False).mean()
    slow_ema = close.ewm(span=slow_window, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=signal_window, adjust=False).mean()
    trend_ema = close.ewm(span=trend_ema_window, adjust=False).mean()
    histogram = macd - signal
    return pd.DataFrame(
        {
            "macd": macd,
            "signal": signal,
            "histogram": histogram,
            "trend_ema": trend_ema,
        }
    )


def _macd_metrics(
    values: pd.Series,
    include_trend_ema: bool,
) -> dict[str, float | str]:
    metrics = {
        "macd": round(float(values["macd"]), 4),
        "signal": round(float(values["signal"]), 4),
        "histogram": round(float(values["histogram"]), 4),
    }
    if include_trend_ema:
        metrics["trend_ema"] = round(float(values["trend_ema"]), 4)
    return metrics


def _passes_macd_trend_filter(
    close: float,
    values: pd.Series,
    direction: TradeDirection,
    enabled: bool,
) -> bool:
    if not enabled:
        return True

    trend_ema = values["trend_ema"]
    if pd.isna(trend_ema):
        return False
    if direction == LONG:
        return close > float(trend_ema)
    return close < float(trend_ema)


def _turtle_levels(
    frame: pd.DataFrame, params: dict[str, Any]
) -> pd.DataFrame | None:
    entry_window = int(params.get("entry_window", 20))
    exit_window = int(params.get("exit_window", 10))
    atr_window = int(params.get("atr_window", 20))
    ma_window = int(params.get("ma_window", 200))

    if exit_window >= entry_window:
        return None
    if len(frame) < max(entry_window, exit_window) + 2:
        return None

    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return pd.DataFrame(
        {
            "entry_high": frame["high"].rolling(entry_window).max().shift(1),
            "entry_low": frame["low"].rolling(entry_window).min().shift(1),
            "exit_high": frame["high"].rolling(exit_window).max().shift(1),
            "exit_low": frame["low"].rolling(exit_window).min().shift(1),
            "atr": true_range.rolling(atr_window, min_periods=1).mean(),
            "moving_average": frame["close"].rolling(ma_window).mean(),
        }
    )


def _latest_turtle_metrics(
    close: float, levels: pd.DataFrame, include_moving_average: bool
) -> dict[str, float | str]:
    return _turtle_metrics(close, levels.iloc[-1], include_moving_average)


def _turtle_metrics(
    close: float, levels: pd.Series, include_moving_average: bool
) -> dict[str, float | str]:
    metrics = {
        "entry_high": round(float(levels["entry_high"]), 4),
        "entry_low": round(float(levels["entry_low"]), 4),
        "exit_high": round(float(levels["exit_high"]), 4),
        "exit_low": round(float(levels["exit_low"]), 4),
        "atr": round(float(levels["atr"]), 4),
    }
    if include_moving_average and not pd.isna(levels["moving_average"]):
        metrics["moving_average"] = round(float(levels["moving_average"]), 4)
    return metrics


def _with_turtle_operation_metrics(
    metrics: dict[str, float | str],
    entry_atr: float | None,
    stop_loss_price: float | None,
    normal_exit_price: float | None,
    exit_price: float | None,
    exit_atr_ratio: float,
    exit_anchor_price: float | None = None,
) -> dict[str, float | str]:
    if entry_atr is None:
        return metrics

    adjusted_metrics = dict(metrics)
    adjusted_metrics["atr"] = round(entry_atr, 4)
    adjusted_metrics["exit_atr_ratio"] = round(exit_atr_ratio, 4)
    if exit_anchor_price is not None:
        adjusted_metrics["exit_anchor"] = round(exit_anchor_price, 4)
    if stop_loss_price is not None:
        adjusted_metrics["stop_loss"] = round(stop_loss_price, 4)
    if normal_exit_price is not None:
        adjusted_metrics["normal_exit"] = round(normal_exit_price, 4)
    if exit_price is not None:
        adjusted_metrics["exit_price"] = round(exit_price, 4)
    return adjusted_metrics


def _current_direction_from_operations(
    operations: list[StrategyOperation],
) -> TradeDirection | None:
    direction = None
    units = 0

    for operation in operations:
        if operation.operation == ENTRY:
            direction = operation.direction
            units = 1
        elif operation.operation == ADD_POSITION and direction == operation.direction:
            units += 1
        elif operation.operation == EXIT and direction == operation.direction:
            direction = None
            units = 0

    return direction


def _exit_detail(direction: TradeDirection, exit_kind: str, exit_atr_ratio: float) -> str:
    if exit_kind == "stop_loss":
        return (
            f"Stop loss exit for {direction} position after price breached the "
            f"last signal price by {exit_atr_ratio:g} ATR."
        )

    return f"Normal exit for {direction} position after the Turtle exit channel was breached."


def _passes_ma_filter(
    close: float,
    levels: pd.Series,
    direction: TradeDirection,
    enabled: bool,
) -> bool:
    if not enabled:
        return True

    moving_average = levels["moving_average"]
    if pd.isna(moving_average):
        return False
    if direction == LONG:
        return close > float(moving_average)
    return close < float(moving_average)


def _turtle_add_operations_for_close(
    trade_date,
    direction: TradeDirection,
    close: float,
    row_levels: pd.Series,
    metrics: dict[str, float | str],
    entry_atr: float | None,
    last_unit_signal_price: float | None,
    units: int,
    max_units: int,
    exit_atr_ratio: float,
    use_ma_filter: bool,
) -> tuple[list[StrategyOperation], int, float | None]:
    operations: list[StrategyOperation] = []
    if not _passes_ma_filter(close, row_levels, direction, use_ma_filter):
        return operations, units, last_unit_signal_price

    while units < max_units:
        add_price = _next_add_price(
            last_unit_signal_price,
            entry_atr,
            direction,
        )
        if add_price is None:
            break
        if direction == LONG:
            if close <= add_price:
                break
            normal_exit_price = float(row_levels["exit_low"])
            detail = "Long breakout continued by 0.5 ATR; adding one Turtle unit."
        else:
            if close >= add_price:
                break
            normal_exit_price = float(row_levels["exit_high"])
            detail = "Short breakout continued by 0.5 ATR; adding one Turtle unit."

        operations.append(
            _make_operation(
                trade_date,
                direction,
                ADD_POSITION,
                close,
                add_price,
                detail,
                _with_turtle_operation_metrics(
                    metrics,
                    entry_atr,
                    _turtle_stop_loss_price(
                        add_price,
                        entry_atr,
                        direction,
                        exit_atr_ratio,
                    ),
                    normal_exit_price,
                    None,
                    exit_atr_ratio,
                    add_price,
                ),
            )
        )
        units += 1
        last_unit_signal_price = add_price

    return operations, units, last_unit_signal_price


def _next_add_price(
    last_unit_signal_price: float | None,
    entry_atr: float | None,
    direction: TradeDirection,
) -> float | None:
    if last_unit_signal_price is None or entry_atr is None or pd.isna(entry_atr):
        return None

    interval = entry_atr * 0.5
    if direction == LONG:
        return round(last_unit_signal_price + interval, 4)
    return round(last_unit_signal_price - interval, 4)


def _turtle_stop_loss_price(
    last_unit_signal_price: float | None,
    entry_atr: float | None,
    direction: TradeDirection,
    exit_atr_ratio: float,
) -> float | None:
    if last_unit_signal_price is None or entry_atr is None or pd.isna(entry_atr):
        return None

    distance = entry_atr * exit_atr_ratio
    if direction == LONG:
        return round(last_unit_signal_price - distance, 4)
    return round(last_unit_signal_price + distance, 4)


def _turtle_exit_levels(
    levels: pd.Series,
    last_unit_signal_price: float | None,
    entry_atr: float | None,
    direction: TradeDirection,
    exit_atr_ratio: float,
) -> dict[str, float | str] | None:
    if last_unit_signal_price is None:
        return None

    stop_loss_price = _turtle_stop_loss_price(
        last_unit_signal_price,
        entry_atr,
        direction,
        exit_atr_ratio,
    )
    normal_exit_price = round(
        float(levels["exit_low"] if direction == LONG else levels["exit_high"]),
        4,
    )

    if stop_loss_price is None:
        return {
            "price": normal_exit_price,
            "kind": "normal_exit",
            "stop_loss_price": None,
            "normal_exit_price": normal_exit_price,
            "anchor_price": last_unit_signal_price,
        }

    if direction == LONG:
        stop_loss_is_active = stop_loss_price >= normal_exit_price
    else:
        stop_loss_is_active = stop_loss_price <= normal_exit_price

    return {
        "price": stop_loss_price if stop_loss_is_active else normal_exit_price,
        "kind": "stop_loss" if stop_loss_is_active else "normal_exit",
        "stop_loss_price": stop_loss_price,
        "normal_exit_price": normal_exit_price,
        "anchor_price": last_unit_signal_price,
    }


def operation_label(operation: OperationType) -> str:
    if operation == ADD_POSITION:
        return "ADD"
    return operation.replace("_", " ").upper()


def _apply_position_sizing(
    operations: list[StrategyOperation], unit_count: int
) -> list[StrategyOperation]:
    balance = INITIAL_BALANCE
    cycle_unit_notional = 0.0
    cycle_unit_risk = 0.0
    current_direction: TradeDirection | None = None
    lots: list[tuple[float, float]] = []
    sized_operations = []

    for operation in operations:
        if operation.operation == ENTRY:
            current_direction = operation.direction
            lots = []
            cycle_unit_notional = balance / max(1, unit_count)
            cycle_unit_risk = balance * POSITION_RISK_PCT
            quantity = _position_quantity(
                operation,
                cycle_unit_notional,
                cycle_unit_risk,
            )
            position_notional = quantity * operation.signal_price
            lots.append((operation.signal_price, quantity))
            sized_operations.append(
                _with_position_size(
                    operation,
                    quantity,
                    position_notional,
                    0.0,
                    balance,
                )
            )
        elif operation.operation == ADD_POSITION:
            if current_direction != operation.direction:
                current_direction = operation.direction
                lots = []
                cycle_unit_notional = balance / max(1, unit_count)
                cycle_unit_risk = balance * POSITION_RISK_PCT

            quantity = _position_quantity(
                operation,
                cycle_unit_notional,
                cycle_unit_risk,
            )
            position_notional = quantity * operation.signal_price
            lots.append((operation.signal_price, quantity))
            sized_operations.append(
                _with_position_size(
                    operation,
                    quantity,
                    position_notional,
                    0.0,
                    balance,
                )
            )
        elif operation.operation == EXIT:
            quantity = sum(lot_quantity for _, lot_quantity in lots)
            realized_pnl = _realized_pnl(operation.direction, operation.signal_price, lots)
            balance += realized_pnl
            sized_operations.append(
                _with_position_size(
                    operation,
                    quantity,
                    quantity * operation.signal_price,
                    realized_pnl,
                    balance,
                )
            )
            current_direction = None
            cycle_unit_notional = 0.0
            lots = []
        else:
            sized_operations.append(operation)

    return sized_operations


def _position_quantity(
    operation: StrategyOperation,
    fallback_notional: float,
    risk_amount: float,
) -> float:
    try:
        volatility = float(operation.metrics.get("atr", 0.0))
    except (TypeError, ValueError):
        volatility = 0.0

    capped_quantity = fallback_notional / operation.signal_price
    if volatility > 0:
        return min(risk_amount / volatility, capped_quantity)

    return capped_quantity


def _realized_pnl(
    direction: TradeDirection,
    exit_price: float,
    lots: list[tuple[float, float]],
) -> float:
    if direction == LONG:
        return sum((exit_price - entry_price) * quantity for entry_price, quantity in lots)
    return sum((entry_price - exit_price) * quantity for entry_price, quantity in lots)


def _with_position_size(
    operation: StrategyOperation,
    quantity: float,
    notional: float,
    realized_pnl: float,
    balance_after: float,
) -> StrategyOperation:
    return replace(
        operation,
        position_size=round(quantity, 8),
        position_notional=round(notional, 2),
        realized_pnl=round(realized_pnl, 2),
        balance_after=round(balance_after, 2),
    )
