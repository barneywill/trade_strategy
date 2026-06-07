from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib import parse, request

from .strategies import StrategyOperation


LOGGER = logging.getLogger(__name__)
TELEGRAM_API_BASE = "https://api.telegram.org"


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    bot_token: str
    chat_id: str

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.bot_token.strip()) and bool(self.chat_id.strip())


def telegram_config(params: dict) -> TelegramConfig:
    return TelegramConfig(
        enabled=bool(params.get("send_telegram_notifications", False)),
        bot_token=str(params.get("telegram_bot_token", "")).strip(),
        chat_id=str(params.get("telegram_chat_id", "")).strip(),
    )


def send_operation_notification(
    config: TelegramConfig,
    ticker,
    strategy_label: str,
    operation: StrategyOperation,
) -> bool:
    if not config.configured:
        return False

    message = format_operation_message(ticker, strategy_label, operation)
    url = f"{TELEGRAM_API_BASE}/bot{parse.quote(config.bot_token)}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": config.chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception:
        LOGGER.exception(
            "Telegram notification failed for %s %s on %s.",
            ticker["display_symbol"],
            strategy_label,
            operation.trade_date,
        )
        return False


def format_operation_message(ticker, strategy_label: str, operation: StrategyOperation) -> str:
    return "\n".join(
        [
            f"{ticker['display_symbol']} {strategy_label}",
            f"Operation: {operation.label}",
            f"Date: {operation.trade_date}",
            f"Signal: {operation.signal_price:.4f}",
            f"Price: {operation.price:.4f}",
            f"Position: {operation.position_size:.3f}",
            f"Balance: ${operation.balance_after:,.2f}",
            operation.detail,
        ]
    )


def operation_notification_key(operation: StrategyOperation) -> str:
    return "|".join(
        [
            operation.trade_date,
            operation.direction,
            operation.operation,
            f"{operation.signal_price:.8f}",
        ]
    )
