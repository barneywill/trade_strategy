from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
)


class NYSEHolidayCalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("New Year's Day", month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas Day", month=12, day=25, observance=nearest_workday),
    ]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def latest_completed_data_date(asset_type: str, now: datetime | None = None) -> date:
    current = now or utc_now()
    candidate = current.date() - timedelta(days=1)

    if asset_type == "stock":
        return previous_us_stock_trading_day(candidate)

    return candidate


def current_realtime_data_date(asset_type: str, now: datetime | None = None) -> date:
    current = now or utc_now()
    if asset_type == "stock":
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(ZoneInfo("America/New_York")).date()
    return current.date()


def is_us_stock_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False

    holidays = NYSEHolidayCalendar().holidays(
        start=pd.Timestamp(day), end=pd.Timestamp(day)
    )
    return pd.Timestamp(day) not in holidays


def is_us_stock_market_open(now: datetime | None = None) -> bool:
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    eastern = current.astimezone(ZoneInfo("America/New_York"))
    if not is_us_stock_trading_day(eastern.date()):
        return False

    return time(9, 30) <= eastern.time() < time(16, 0)


def previous_us_stock_trading_day(day: date) -> date:
    candidate = day
    while not is_us_stock_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def seconds_until_next_utc_hour(
    hour: int, now: datetime | None = None
) -> float:
    return seconds_until_next_utc_time(hour, 0, now)


def seconds_until_next_utc_time(
    hour: int,
    minute: int = 0,
    now: datetime | None = None,
) -> float:
    current = now or utc_now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return max(0.0, (target - current).total_seconds())
