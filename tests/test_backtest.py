import pandas as pd

from trade_strategy.backtest import yearly_backtest
from trade_strategy.strategies import EMACrossoverStrategy


def test_yearly_backtest_compares_strategy_with_buy_and_hold():
    history = pd.DataFrame(
        {"close": [14, 13, 12, 11, 10, 11, 13, 15, 14, 12, 10, 8]},
        index=pd.date_range("2024-01-01", periods=12),
    )
    operations = EMACrossoverStrategy().operation_history(
        history,
        {"fast_window": 2, "slow_window": 4},
    )

    result = yearly_backtest(history, operations)

    assert result.strategy_final == 10069.93
    assert result.buy_hold_final == 5714.29
    assert result.strategy_total_return_pct == 0.7
    assert result.buy_hold_total_return_pct == -42.86
    assert len(result.yearly) == 1
    assert result.yearly[0].difference_pct == 43.56
