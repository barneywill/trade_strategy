from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


DEFAULT_LOG_DIR = Path("/tmp/trade_strategy")
LOG_FILE_NAME = "trade_strategy.log"


def configure_file_logging(log_dir: str | Path | None = None) -> Path:
    directory = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    log_file = directory / LOG_FILE_NAME

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in root_logger.handlers:
        if getattr(handler, "_trade_strategy_log_file", None) == str(log_file):
            return log_file

    for handler in list(root_logger.handlers):
        if getattr(handler, "_trade_strategy_file_handler", False):
            root_logger.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(
        log_file,
        maxBytes=5_000_000,
        backupCount=3,
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    )
    handler._trade_strategy_file_handler = True
    handler._trade_strategy_log_file = str(log_file)
    root_logger.addHandler(handler)
    return log_file
