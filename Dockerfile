FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MUSICBOX_DB=/data/musicbox.db \
    PORT=8000 \
    SEED_DEMO=1

RUN mkdir -p /data
EXPOSE 8000

CMD ["python", "app.py"]
