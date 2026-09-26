FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY bot.py /app/bot.py
RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1 \
    BOT_TIMEZONE=Asia/Tomsk \
    MORNING_HOUR=9 \
    DATABASE_PATH=/data/expenses.db

CMD ["python", "bot.py"]
