from datetime import datetime, timezone

from trade_strategy.market_calendar import (
    is_us_stock_market_open,
    is_us_stock_trading_day,
    latest_completed_data_date,
    previous_us_stock_trading_day,
    seconds_until_next_utc_time,
)


def test_us_stock_calendar_skips_weekends_and_market_holidays():
    assert is_us_stock_trading_day(datetime(2025, 7, 3).date())
    assert not is_us_stock_trading_day(datetime(2025, 7, 4).date())
    assert not is_us_stock_trading_day(datetime(2025, 7, 5).date())


def test_previous_us_stock_trading_day_skips_observed_holiday():
    assert previous_us_stock_trading_day(datetime(2025, 7, 4).date()).isoformat() == "2025-07-03"


def test_latest_completed_data_date_uses_stock_trading_calendar():
    now = datetime(2025, 7, 5, 1, 0, tzinfo=timezone.utc)

    assert latest_completed_data_date("stock", now).isoformat() == "2025-07-03"
    assert latest_completed_data_date("crypto", now).isoformat() == "2025-07-04"


def test_us_stock_market_open_uses_regular_session_hours():
    open_time = datetime(2025, 7, 3, 14, 0, tzinfo=timezone.utc)
    before_open = datetime(2025, 7, 3, 13, 0, tzinfo=timezone.utc)
    holiday = datetime(2025, 7, 4, 14, 0, tzinfo=timezone.utc)

    assert is_us_stock_market_open(open_time)
    assert not is_us_stock_market_open(before_open)
    assert not is_us_stock_market_open(holiday)


def test_seconds_until_next_utc_time_supports_minutes():
    now = datetime(2026, 6, 7, 0, 0, 30, tzinfo=timezone.utc)
    after_target = datetime(2026, 6, 7, 0, 2, 0, tzinfo=timezone.utc)

    assert seconds_until_next_utc_time(0, 1, now) == 30
    assert seconds_until_next_utc_time(0, 1, after_target) == 86340
