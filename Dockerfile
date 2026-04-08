# ── Nexus Trading System — Python Backend ────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: shadow mode (no live trading, no broker keys needed)
ENV TRADING_MODE=shadow
ENV REDIS_URL=redis://redis:6379/0
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py", "--mode", "shadow"]
