# Scanner 15m + ejecutor — servicio aparte del bot de deportes.
# Se construye SOLO con la carpeta scanner15/ (Root Directory = scanner15 en Railway).
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1 \
    SCANNER_DATA_DIR=/data \
    POLY_BOT_DIR=/app \
    TZ=America/Mexico_City

CMD ["bash", "start.sh"]
