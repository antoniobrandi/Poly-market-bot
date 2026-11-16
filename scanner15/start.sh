#!/usr/bin/env bash
# Arranque en el servidor: scanner (sin pantalla) + ejecutor, en el mismo contenedor.
# Si cualquiera de los dos muere, el contenedor sale y Railway lo reinicia.
set -u
DATA="${SCANNER_DATA_DIR:-/data}"
mkdir -p "$DATA"

COINS_ARGS=()
if [ -n "${SCANNER_COINS:-}" ]; then COINS_ARGS=(--coins $SCANNER_COINS); fi

LIVE_ARGS=()
if [ "${EXECUTOR_LIVE:-false}" = "true" ]; then
  LIVE_ARGS=(--live)
  echo "⚠️  EXECUTOR_LIVE=true — el ejecutor opera con DINERO REAL"
else
  echo "🧪 Ejecutor en DRY_RUN (EXECUTOR_LIVE no es 'true')"
fi

if [ -n "${DATA_TOKEN:-}" ]; then python webstatus.py & fi
python scanner.py --headless "${COINS_ARGS[@]}" &
python executor.py --follow "$DATA/senales.csv" "${LIVE_ARGS[@]}" &

wait -n
echo "Un proceso terminó; salgo para que Railway reinicie el servicio."
exit 1
