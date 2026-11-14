FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data /app/tmp \
    && chown -R bot:bot /app
USER bot

ENV PYTHONUNBUFFERED=1 DB_PATH=/app/data/bot.db TMP_DIR=/app/tmp
CMD ["python", "-m", "bot.main"]
