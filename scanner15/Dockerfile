# Scanner 15m + ejecutor — servicio aparte del bot de deportes.
# Se construye desde la RAÍZ del repo (necesita clob_conn.py).
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY scanner15/requirements.txt scanner15/requirements.txt
RUN pip install --no-cache-dir -r scanner15/requirements.txt

COPY clob_conn.py ./
COPY scanner15/ scanner15/
WORKDIR /app/scanner15

ENV PYTHONUNBUFFERED=1 \
    SCANNER_DATA_DIR=/data \
    POLY_BOT_DIR=/app \
    TZ=America/Mexico_City

CMD ["bash", "start.sh"]
