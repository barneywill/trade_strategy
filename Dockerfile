FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    TRADE_STRATEGY_DB=/app/data/trade_strategy.sqlite3 \
    TRADE_STRATEGY_AUTO_REFRESH=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt gunicorn

COPY src ./src
COPY data ./data
COPY README.md ./README.md

EXPOSE 5001

CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "1", "--threads", "4", "--timeout", "120", "trade_strategy.app:create_app()"]
