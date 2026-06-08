from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from . import database
from .market_calendar import latest_completed_data_date
from .strategies import (
    ADD_POSITION,
    ENTRY,
    EXIT,
    LONG,
    OperationType,
    SHORT,
    STRATEGIES,
    TurtleBreakoutStrategy,
    StrategyOperation,
    TradeStrategy,
    _next_add_price,
    _turtle_exit_levels,
    _turtle_levels,
)


_REALTIME_TRIGGER_STATES: dict[
    tuple[str, int, str], "_CachedRealtimeTriggerState"
] = {}


@dataclass(frozen=True)
class _CachedRealtimeTriggerState:
    params_key: str
    realtime_date: str | None
    state: "_RealtimeTriggerState"


@dataclass(frozen=True)
class RealtimeOperationCandidate:
    direction: str
    operation: OperationType
    signal_price: float | None = None


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
        asset_type: str | None = None,
    ) -> None:
        configs = configs or database.list_strategy_configs(self.db_path)
        history = database.load_history(ticker_id, self.db_path)
        for strategy_name, strategy in STRATEGIES.items():
            config = configs.get(strategy_name)
            if config is None:
                config = {"enabled": True, "params": strategy.default_params}
            if config["enabled"]:
                self.operations_for(
                    ticker_id,
                    strategy_name,
                    strategy,
                    _completed_strategy_history(history, strategy_name, asset_type),
                    config["params"],
                )
                if strategy_name == "turtle_breakout":
                    self.invalidate_realtime_trigger_state(ticker_id, strategy_name)

    def refresh_all(self, configs: dict[str, dict[str, Any]] | None = None) -> None:
        configs = configs or database.list_strategy_configs(self.db_path)
        for ticker in database.list_tickers(self.db_path):
            self.refresh_ticker(int(ticker["id"]), configs, ticker["asset_type"])

    def realtime_operation_triggered(
        self,
        ticker_id: int,
        current_price: float,
        configs: dict[str, dict[str, Any]] | None = None,
        realtime_date: date | None = None,
    ) -> bool:
        return bool(
            self.realtime_operation_candidates(
                ticker_id,
                current_price,
                configs,
                realtime_date,
            )
        )

    def realtime_operation_candidates(
        self,
        ticker_id: int,
        current_price: float,
        configs: dict[str, dict[str, Any]] | None = None,
        realtime_date: date | None = None,
    ) -> dict[str, list[RealtimeOperationCandidate]]:
        configs = configs or database.list_strategy_configs(self.db_path)
        strategy_name = "turtle_breakout"
        strategy = STRATEGIES[strategy_name]
        config = configs.get(strategy_name)
        if config is None:
            config = {"enabled": True, "params": strategy.default_params}
        if not config["enabled"]:
            return {}

        state = self._turtle_realtime_trigger_state(
            ticker_id,
            strategy_name,
            strategy,
            config["params"],
            realtime_date,
        )
        if state is None:
            return {}

        operations = state.triggered_operations(float(current_price))
        return {strategy_name: operations} if operations else {}

    def invalidate_realtime_trigger_state(
        self,
        ticker_id: int,
        strategy_name: str = "turtle_breakout",
    ) -> None:
        _REALTIME_TRIGGER_STATES.pop((str(self.db_path), ticker_id, strategy_name), None)

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

    def _turtle_realtime_trigger_state(
        self,
        ticker_id: int,
        strategy_name: str,
        strategy: TradeStrategy,
        params: dict[str, Any],
        realtime_date: date | None,
    ) -> "_RealtimeTriggerState | None":
        params_key = params_signature(params)
        realtime_date_key = realtime_date.isoformat() if realtime_date else None
        state_key = (str(self.db_path), ticker_id, strategy_name)
        cached = _REALTIME_TRIGGER_STATES.get(state_key)
        if (
            cached is not None
            and cached.params_key == params_key
            and cached.realtime_date == realtime_date_key
        ):
            return cached.state

        history = database.load_history(ticker_id, self.db_path)
        if realtime_date is not None:
            history = _completed_history_before(history, realtime_date)
        if history.empty:
            _REALTIME_TRIGGER_STATES.pop(state_key, None)
            return None

        cache_key = operation_cache_key(strategy_name, params, history)
        operations = self._load_operations(ticker_id, strategy_name)
        if not operations:
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
            _REALTIME_TRIGGER_STATES[state_key] = _CachedRealtimeTriggerState(
                params_key=params_key,
                realtime_date=realtime_date_key,
                state=state,
            )
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


def params_signature(params: dict[str, Any]) -> str:
    raw_payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
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


def _completed_history_before(history: pd.DataFrame, realtime_date: date) -> pd.DataFrame:
    if history.empty:
        return history
    return history[history.index.date < realtime_date]


def _completed_strategy_history(
    history: pd.DataFrame,
    strategy_name: str,
    asset_type: str | None,
) -> pd.DataFrame:
    if strategy_name == "turtle_breakout" or asset_type is None or history.empty:
        return history

    completed_date = latest_completed_data_date(asset_type)
    return history[history.index.date <= completed_date]


@dataclass(frozen=True)
class _RealtimeTriggerState:
    cache_key: str

    def triggered_operations(
        self, current_price: float
    ) -> list[RealtimeOperationCandidate]:
        return []


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

    def triggered_operations(
        self, current_price: float
    ) -> list[RealtimeOperationCandidate]:
        if self.direction is None:
            if current_price > self.entry_high:
                return (
                    [RealtimeOperationCandidate(LONG, ENTRY)]
                    if self._passes_filter(current_price, LONG)
                    else []
                )
            if current_price < self.entry_low:
                return (
                    [RealtimeOperationCandidate(SHORT, ENTRY)]
                    if self._passes_filter(current_price, SHORT)
                    else []
                )
            return []

        exit_price = self._exit_price()
        if self.direction == LONG:
            if exit_price is not None and current_price <= exit_price:
                return [RealtimeOperationCandidate(LONG, EXIT)]
            add_price = _next_add_price(
                self.last_unit_signal_price,
                self.entry_atr,
                LONG,
            )
            should_add = (
                add_price is not None
                and current_price > add_price
                and self.units < self.max_units
                and self._passes_filter(current_price, LONG)
            )
            return (
                [RealtimeOperationCandidate(LONG, ADD_POSITION, add_price)]
                if should_add
                else []
            )

        exit_price = self._exit_price()
        if exit_price is not None and current_price >= exit_price:
            return [RealtimeOperationCandidate(SHORT, EXIT)]
        add_price = _next_add_price(
            self.last_unit_signal_price,
            self.entry_atr,
            SHORT,
        )
        should_add = (
            add_price is not None
            and current_price < add_price
            and self.units < self.max_units
            and self._passes_filter(current_price, SHORT)
        )
        return (
            [RealtimeOperationCandidate(SHORT, ADD_POSITION, add_price)]
            if should_add
            else []
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
    return None


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
