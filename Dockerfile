FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py investments.py /app/
RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1 \
    BOT_TIMEZONE=Asia/Tomsk \
    MORNING_HOUR=9 \
    DATABASE_PATH=/data/expenses.db

CMD ["python", "bot.py"]
