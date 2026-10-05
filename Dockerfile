FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# Ready checks are saved here. Mount a volume or they are lost on recreate.
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1 \
    PERSISTENCE_FILE=/data/dotabot.pickle
CMD ["python", "bot.py"]
