FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 TZ=UTC
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# state/ and logs/ must be on a persistent volume (halt flag, last processed candle, trade log)
VOLUME ["/app/state", "/app/logs"]
CMD ["python", "bot.py"]
