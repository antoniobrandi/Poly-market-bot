# Bot de Polymarket — imagen explícita.
#
# El repo tiene archivos de Node (package.json, pnpm-lock.yaml) heredados
# de Replit. Sin este Dockerfile, el autodetector de Railway cree que es un
# proyecto Node y falla: el script "preinstall" del package.json aborta
# cuando no se usa pnpm. Con el Dockerfile, el build es solo Python.

# Python 3.13: misma versión que ya corre en el servidor Hetzner, donde
# todas las dependencias instalan desde wheels (sin compilar nada).
FROM python:3.13-slim

WORKDIR /app

# Dependencias primero para aprovechar la cache de capas
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Logs sin buffer: aparecen en Railway al instante
ENV PYTHONUNBUFFERED=1
# Estado persistente en el Volume montado (ver deploy/README.md)
ENV DATA_DIR=/data

CMD ["python", "polymarket_agent.py"]
