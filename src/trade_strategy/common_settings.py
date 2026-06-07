from __future__ import annotations

from .strategies import ParameterSpec


COMMON_CONFIG_NAME = "__common__"
COMMON_PARAMETER_SPECS = (
    ParameterSpec(
        "default_group_symbols",
        "Default group tickers",
        "BTC, ETH, SOL, QQQ, SPY",
        kind="text",
    ),
    ParameterSpec(
        "enable_realtime_updates",
        "Realtime trading data update",
        False,
        kind="checkbox",
    ),
    ParameterSpec(
        "realtime_update_frequency",
        "Realtime update frequency (seconds)",
        300,
        minimum=30,
        maximum=86400,
    ),
    ParameterSpec(
        "daily_data_fetch_time",
        "Daily data fetch time (UTC)",
        "00:01",
        kind="time",
    ),
    ParameterSpec(
        "send_telegram_notifications",
        "Send Notification to Telegram",
        False,
        kind="checkbox",
    ),
    ParameterSpec(
        "telegram_bot_token",
        "Telegram bot token",
        "",
        kind="password",
    ),
    ParameterSpec(
        "telegram_chat_id",
        "Telegram chat ID",
        "",
        kind="text",
    ),
)
COMMON_DEFAULTS = {
    parameter.name: parameter.default for parameter in COMMON_PARAMETER_SPECS
}


def common_params(configs: dict[str, dict]) -> dict:
    config = configs.get(COMMON_CONFIG_NAME, {"params": COMMON_DEFAULTS})
    return {**COMMON_DEFAULTS, **config.get("params", {})}
