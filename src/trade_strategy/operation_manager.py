from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from . import database
from .strategies import (
    ADD_POSITION,
    EMACrossoverStrategy,
    ENTRY,
    EXIT,
    LONG,
    MACDTrendFollowingStrategy,
    SHORT,
    STRATEGIES,
    TurtleBreakoutStrategy,
    StrategyOperation,
    TradeStrategy,
    _current_direction_from_operations,
    _ema_values,
    _macd_values,
    _next_add_price,
    _passes_ema_trend_filter,
    _passes_macd_trend_filter,
    _turtle_exit_levels,
    _turtle_levels,
)


_REALTIME_TRIGGER_STATES: dict[
    tuple[str, int, str], "_RealtimeTriggerState"
] = {}


class OperationManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def operations_for(
        self,
        ticker_id: int,
        strategy_name: str,
        strategy: TradeStrategy,
        history: pd.DataFrame,
        params: dict[str, Any],
    ) -> list[StrategyOperation]:
        cache_key = operation_cache_key(strategy_name, params, history)
        saved_cache_key = database.get_operation_cache_key(
            ticker_id,
            strategy_name,
            self.db_path,
        )
        if saved_cache_key == cache_key:
            return self._load_operations(ticker_id, strategy_name)

        operations = strategy.operation_history(history, params)
        database.save_strategy_operations(
            ticker_id,
            strategy_name,
            operations,
            cache_key,
            self.db_path,
        )
        return operations

    def refresh_ticker(
        self,
        ticker_id: int,
        configs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        configs = configs or database.list_strategy_configs(self.db_path)
        history = database.load_history(ticker_id, self.db_path)
        for strategy_name, strategy in STRATEGIES.items():
            config = configs.get(
                strategy_name,
                {"enabled": True, "params": strategy.default_params},
            )
            if config["enabled"]:
                self.operations_for(
                    ticker_id,
                    strategy_name,
                    strategy,
                    history,
                    config["params"],
                )

    def refresh_all(self, configs: dict[str, dict[str, Any]] | None = None) -> None:
        configs = configs or database.list_strategy_configs(self.db_path)
        for ticker in database.list_tickers(self.db_path):
            self.refresh_ticker(int(ticker["id"]), configs)

    def realtime_operation_triggered(
        self,
        ticker_id: int,
        current_price: float,
        configs: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        configs = configs or database.list_strategy_configs(self.db_path)
        history = database.load_history(ticker_id, self.db_path)
        if history.empty:
            return False

        for strategy_name, strategy in STRATEGIES.items():
            config = configs.get(
                strategy_name,
                {"enabled": True, "params": strategy.default_params},
            )
            if not config["enabled"]:
                continue

            state = self._realtime_trigger_state(
                ticker_id,
                strategy_name,
                strategy,
                history,
                config["params"],
            )
            if state is not None and state.triggered(float(current_price)):
                return True

        return False

    def _load_operations(
        self,
        ticker_id: int,
        strategy_name: str,
    ) -> list[StrategyOperation]:
        return [
            StrategyOperation(
                trade_date=row["trade_date"],
                direction=row["direction"],
                operation=row["operation"],
                price=row["price"],
                signal_price=row["signal_price"],
                detail=row["detail"],
                metrics=json.loads(row["metrics_json"]),
                signal_class=row["signal_class"],
                position_size=row["position_size"],
                position_notional=row["position_notional"],
                realized_pnl=row["realized_pnl"],
                balance_after=row["balance_after"],
            )
            for row in database.load_strategy_operations(
                ticker_id,
                strategy_name,
                self.db_path,
            )
        ]

    def _realtime_trigger_state(
        self,
        ticker_id: int,
        strategy_name: str,
        strategy: TradeStrategy,
        history: pd.DataFrame,
        params: dict[str, Any],
    ) -> "_RealtimeTriggerState | None":
        cache_key = operation_cache_key(strategy_name, params, history)
        state_key = (str(self.db_path), ticker_id, strategy_name)
        state = _REALTIME_TRIGGER_STATES.get(state_key)
        if state is not None and state.cache_key == cache_key:
            return state

        operations = self.operations_for(
            ticker_id,
            strategy_name,
            strategy,
            history,
            params,
        )
        state = _build_realtime_trigger_state(
            strategy_name,
            strategy,
            history,
            params,
            operations,
            cache_key,
        )
        if state is None:
            _REALTIME_TRIGGER_STATES.pop(state_key, None)
        else:
            _REALTIME_TRIGGER_STATES[state_key] = state
        return state


def operation_cache_key(
    strategy_name: str,
    params: dict[str, Any],
    history: pd.DataFrame,
) -> str:
    payload = {
        "strategy_name": strategy_name,
        "params": params,
        "history": history_signature(history),
    }
    raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()


def history_signature(history: pd.DataFrame) -> dict[str, Any]:
    frame = history.dropna(subset=["close"]).copy()
    if frame.empty:
        return {"rows": 0}

    latest = frame.iloc[-1]
    return {
        "rows": len(frame),
        "first_date": frame.index[0].date().isoformat(),
        "last_date": frame.index[-1].date().isoformat(),
        "last_open": _optional_float(latest.get("open")),
        "last_high": _optional_float(latest.get("high")),
        "last_low": _optional_float(latest.get("low")),
        "last_close": _optional_float(latest.get("close")),
        "last_volume": _optional_float(latest.get("volume")),
    }


def _optional_float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 8)


@dataclass(frozen=True)
class _RealtimeTriggerState:
    cache_key: str

    def triggered(self, current_price: float) -> bool:
        return False


@dataclass(frozen=True)
class _EmaRealtimeTriggerState(_RealtimeTriggerState):
    direction: str | None
    previous_fast: float
    previous_slow: float
    prior_fast_before_latest: float
    prior_slow_before_latest: float
    prior_trend_ema: float
    fast_alpha: float
    slow_alpha: float
    trend_alpha: float
    use_trend_filter: bool

    def triggered(self, current_price: float) -> bool:
        fast = self.fast_alpha * current_price + (1.0 - self.fast_alpha) * self.prior_fast_before_latest
        slow = self.slow_alpha * current_price + (1.0 - self.slow_alpha) * self.prior_slow_before_latest
        trend_ema = self.trend_alpha * current_price + (1.0 - self.trend_alpha) * self.prior_trend_ema
        values = pd.Series(
            {
                "fast_ema": fast,
                "slow_ema": slow,
                "trend_ema": trend_ema,
            }
        )
        crossed_long = self.previous_fast <= self.previous_slow and fast > slow
        crossed_short = self.previous_fast >= self.previous_slow and fast < slow
        long_direction = fast > slow
        short_direction = fast < slow
        long_allowed = _passes_ema_trend_filter(
            current_price,
            values,
            LONG,
            self.use_trend_filter,
        )
        short_allowed = _passes_ema_trend_filter(
            current_price,
            values,
            SHORT,
            self.use_trend_filter,
        )

        if self.direction == LONG:
            return crossed_short or not long_allowed
        if self.direction == SHORT:
            return crossed_long or not short_allowed
        return (long_direction and long_allowed) or (short_direction and short_allowed)


@dataclass(frozen=True)
class _MacdRealtimeTriggerState(_RealtimeTriggerState):
    direction: str | None
    previous_macd: float
    previous_signal: float
    prior_fast_ema: float
    prior_slow_ema: float
    prior_signal: float
    prior_trend_ema: float
    fast_alpha: float
    slow_alpha: float
    signal_alpha: float
    trend_alpha: float
    use_trend_filter: bool

    def triggered(self, current_price: float) -> bool:
        fast_ema = self.fast_alpha * current_price + (1.0 - self.fast_alpha) * self.prior_fast_ema
        slow_ema = self.slow_alpha * current_price + (1.0 - self.slow_alpha) * self.prior_slow_ema
        macd = fast_ema - slow_ema
        signal = self.signal_alpha * macd + (1.0 - self.signal_alpha) * self.prior_signal
        trend_ema = self.trend_alpha * current_price + (1.0 - self.trend_alpha) * self.prior_trend_ema
        values = pd.Series({"macd": macd, "signal": signal, "trend_ema": trend_ema})
        crossed_long = self.previous_macd <= self.previous_signal and macd > signal
        crossed_short = self.previous_macd >= self.previous_signal and macd < signal
        long_allowed = _passes_macd_trend_filter(
            current_price,
            values,
            LONG,
            self.use_trend_filter,
        )
        short_allowed = _passes_macd_trend_filter(
            current_price,
            values,
            SHORT,
            self.use_trend_filter,
        )

        if self.direction == LONG:
            return crossed_short or not long_allowed
        if self.direction == SHORT:
            return crossed_long or not short_allowed
        return (crossed_long and long_allowed) or (crossed_short and short_allowed)


@dataclass(frozen=True)
class _TurtleRealtimeTriggerState(_RealtimeTriggerState):
    direction: str | None
    units: int
    max_units: int
    entry_high: float
    entry_low: float
    exit_high: float
    exit_low: float
    moving_average: float | None
    use_ma_filter: bool
    last_unit_signal_price: float | None
    entry_atr: float | None
    exit_atr_ratio: float

    def triggered(self, current_price: float) -> bool:
        if self.direction is None:
            if current_price > self.entry_high:
                return self._passes_filter(current_price, LONG)
            if current_price < self.entry_low:
                return self._passes_filter(current_price, SHORT)
            return False

        exit_price = self._exit_price()
        if self.direction == LONG:
            if exit_price is not None and current_price <= exit_price:
                return True
            add_price = _next_add_price(
                self.last_unit_signal_price,
                self.entry_atr,
                LONG,
            )
            return (
                add_price is not None
                and current_price > add_price
                and self.units < self.max_units
                and self._passes_filter(current_price, LONG)
            )

        exit_price = self._exit_price()
        if exit_price is not None and current_price >= exit_price:
            return True
        add_price = _next_add_price(
            self.last_unit_signal_price,
            self.entry_atr,
            SHORT,
        )
        return (
            add_price is not None
            and current_price < add_price
            and self.units < self.max_units
            and self._passes_filter(current_price, SHORT)
        )

    def _passes_filter(self, current_price: float, direction: str) -> bool:
        if not self.use_ma_filter:
            return True
        if self.moving_average is None:
            return False
        if direction == LONG:
            return current_price > self.moving_average
        return current_price < self.moving_average

    def _exit_price(self) -> float | None:
        if self.direction is None:
            return None
        levels = pd.Series(
            {
                "exit_low": self.exit_low,
                "exit_high": self.exit_high,
            }
        )
        exit_levels = _turtle_exit_levels(
            levels,
            self.last_unit_signal_price,
            self.entry_atr,
            self.direction,
            self.exit_atr_ratio,
        )
        if exit_levels is None:
            return None
        return float(exit_levels["price"])


def _build_realtime_trigger_state(
    strategy_name: str,
    strategy: TradeStrategy,
    history: pd.DataFrame,
    params: dict[str, Any],
    operations: list[StrategyOperation],
    cache_key: str,
) -> _RealtimeTriggerState | None:
    if isinstance(strategy, TurtleBreakoutStrategy) or strategy_name == "turtle_breakout":
        return _build_turtle_realtime_trigger_state(history, params, operations, cache_key)
    if isinstance(strategy, MACDTrendFollowingStrategy) or strategy_name == "macd_trend_following":
        return _build_macd_realtime_trigger_state(history, params, operations, cache_key)
    if isinstance(strategy, EMACrossoverStrategy) or strategy_name == "ema_crossover":
        return _build_ema_realtime_trigger_state(history, params, operations, cache_key)
    return None


def _build_ema_realtime_trigger_state(
    history: pd.DataFrame,
    params: dict[str, Any],
    operations: list[StrategyOperation],
    cache_key: str,
) -> _EmaRealtimeTriggerState | None:
    frame = history.dropna(subset=["close"]).copy()
    fast_window = int(params.get("fast_window", 12))
    slow_window = int(params.get("slow_window", 26))
    trend_ema_window = int(params.get("trend_ema_window", 200))
    values = _ema_values(frame, params)
    if values is None or len(values) < 2:
        return None

    close = frame["close"].astype(float)
    fast = close.ewm(span=fast_window, adjust=False).mean()
    slow = close.ewm(span=slow_window, adjust=False).mean()
    trend = close.ewm(span=trend_ema_window, adjust=False).mean()
    return _EmaRealtimeTriggerState(
        cache_key=cache_key,
        direction=_current_direction_from_operations(operations),
        previous_fast=float(fast.iloc[-2]),
        previous_slow=float(slow.iloc[-2]),
        prior_fast_before_latest=float(fast.iloc[-2]),
        prior_slow_before_latest=float(slow.iloc[-2]),
        prior_trend_ema=float(trend.iloc[-2]),
        fast_alpha=2.0 / (fast_window + 1.0),
        slow_alpha=2.0 / (slow_window + 1.0),
        trend_alpha=2.0 / (trend_ema_window + 1.0),
        use_trend_filter=bool(params.get("use_trend_filter", False)),
    )


def _build_macd_realtime_trigger_state(
    history: pd.DataFrame,
    params: dict[str, Any],
    operations: list[StrategyOperation],
    cache_key: str,
) -> _MacdRealtimeTriggerState | None:
    frame = history.dropna(subset=["close"]).copy()
    fast_window = int(params.get("fast_window", 12))
    slow_window = int(params.get("slow_window", 26))
    signal_window = int(params.get("signal_window", 9))
    trend_ema_window = int(params.get("trend_ema_window", 200))
    values = _macd_values(frame, params)
    if values is None or len(values) < 2:
        return None

    close = frame["close"].astype(float)
    fast = close.ewm(span=fast_window, adjust=False).mean()
    slow = close.ewm(span=slow_window, adjust=False).mean()
    previous = values.iloc[-2]
    direction = _current_direction_from_operations(operations)
    return _MacdRealtimeTriggerState(
        cache_key=cache_key,
        direction=direction,
        previous_macd=float(previous["macd"]),
        previous_signal=float(previous["signal"]),
        prior_fast_ema=float(fast.iloc[-2]),
        prior_slow_ema=float(slow.iloc[-2]),
        prior_signal=float(previous["signal"]),
        prior_trend_ema=float(values["trend_ema"].iloc[-2]),
        fast_alpha=2.0 / (fast_window + 1.0),
        slow_alpha=2.0 / (slow_window + 1.0),
        signal_alpha=2.0 / (signal_window + 1.0),
        trend_alpha=2.0 / (trend_ema_window + 1.0),
        use_trend_filter=bool(params.get("use_trend_filter", True)),
    )


def _build_turtle_realtime_trigger_state(
    history: pd.DataFrame,
    params: dict[str, Any],
    operations: list[StrategyOperation],
    cache_key: str,
) -> _TurtleRealtimeTriggerState | None:
    frame = history.dropna(subset=["close", "high", "low"]).copy()
    levels = _turtle_levels(frame, params)
    if levels is None or levels.empty:
        return None
    latest_levels = levels.iloc[-1]
    required = ["entry_high", "entry_low", "exit_high", "exit_low"]
    if latest_levels[required].isna().any():
        return None

    direction, units, last_unit_signal_price, entry_atr = _open_turtle_state(operations)
    use_ma_filter = bool(params.get("use_ma_filter", False))
    moving_average = latest_levels["moving_average"]
    return _TurtleRealtimeTriggerState(
        cache_key=cache_key,
        direction=direction,
        units=units,
        max_units=max(1, int(params.get("max_units", 4))),
        entry_high=float(latest_levels["entry_high"]),
        entry_low=float(latest_levels["entry_low"]),
        exit_high=float(latest_levels["exit_high"]),
        exit_low=float(latest_levels["exit_low"]),
        moving_average=None if pd.isna(moving_average) else float(moving_average),
        use_ma_filter=use_ma_filter,
        last_unit_signal_price=last_unit_signal_price,
        entry_atr=entry_atr,
        exit_atr_ratio=float(params.get("exit_atr_ratio", 2.0)),
    )


def _open_turtle_state(
    operations: list[StrategyOperation],
) -> tuple[str | None, int, float | None, float | None]:
    direction = None
    units = 0
    last_unit_signal_price = None
    entry_atr = None

    for operation in operations:
        if operation.operation == ENTRY:
            direction = operation.direction
            units = 1
            last_unit_signal_price = operation.signal_price
            entry_atr = _operation_metric_float(operation, "atr")
        elif operation.operation == ADD_POSITION and operation.direction == direction:
            units += 1
            last_unit_signal_price = operation.signal_price
        elif operation.operation == EXIT and operation.direction == direction:
            direction = None
            units = 0
            last_unit_signal_price = None
            entry_atr = None

    return direction, units, last_unit_signal_price, entry_atr


def _operation_metric_float(operation: StrategyOperation, key: str) -> float | None:
    value = operation.metrics.get(key)
    if value is None or pd.isna(value):
        return None
    return float(value)
