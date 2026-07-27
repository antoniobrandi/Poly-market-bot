# Deploy 24/7 en Hetzner (systemd)

Guía para dejar el bot corriendo solo, sin la laptop. Todos los comandos
van en el servidor por SSH.

---

## 1. Instalar

```bash
# Como root (o con sudo)
adduser --system --group --home /opt/poly-market-bot botuser

cd /opt
git clone https://github.com/antoniobrandi/Poly-market-bot.git poly-market-bot
cd poly-market-bot

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

## 2. Configurar

```bash
cp .env.example .env
nano .env
```

Rellena como mínimo:
- `POLYMARKET_PRIVATE_KEY` y `POLYMARKET_PROXY_ADDRESS`
- **Deja `DRY_RUN=true`** para la primera prueba
- Deja `FREE_ONLY=true` (garantiza cero costos de API)
- Deja `KEEPALIVE_ENABLED=false` (evita chocar con el bot de Telegram)

Permisos (el `.env` tiene tu private key):
```bash
chmod 600 .env
chown -R botuser:botuser /opt/poly-market-bot
```

## 3. Instalar el servicio

```bash
cp deploy/polymarket-bot.service /etc/systemd/system/
# Revisa que User/WorkingDirectory/ExecStart coincidan con tu instalación
nano /etc/systemd/system/polymarket-bot.service

systemctl daemon-reload
systemctl enable --now polymarket-bot
```

## 4. Ver que funcione

```bash
systemctl status polymarket-bot
journalctl -u polymarket-bot -f       # logs en vivo (Ctrl+C para salir)
```

En el arranque deberías ver:
```
💸 FREE_ONLY activo — cero llamadas de pago
Estrategias activas: SmartMoney, ResoluciónTardía
[SmartMoney] 4 wallets cargadas (wallets.json)
Loop: tick copytrading cada 15 min | scan completo cada 60 min
```

## 5. Pasar a dinero real

**Deja el bot en `DRY_RUN=true` unas horas** y revisa los logs. Si copia
posiciones simuladas sin errores, entonces:

```bash
nano .env          # DRY_RUN=false
systemctl restart polymarket-bot
journalctl -u polymarket-bot -f
```

Verifica la primera compra real en <https://polymarket.com/portfolio>.

---

## Comandos del día a día

```bash
systemctl status polymarket-bot        # ¿está vivo?
systemctl restart polymarket-bot       # reiniciar (guarda estado antes)
systemctl stop polymarket-bot          # parar
journalctl -u polymarket-bot -f        # logs en vivo
journalctl -u polymarket-bot --since "1 hour ago"
journalctl -u polymarket-bot | grep "PnL\|Copia abierta"   # resultados
```

## Actualizar el bot

```bash
cd /opt/poly-market-bot
systemctl stop polymarket-bot
cp agent_state.json agent_state.json.bak     # backup del estado
git pull
./venv/bin/pip install -r requirements.txt   # por si cambiaron deps
systemctl start polymarket-bot
```

---

## Notas importantes

**El estado sobrevive reinicios.** `agent_state.json` (bankroll, posiciones
abiertas, PnL por wallet) vive en `/opt/poly-market-bot` y se guarda en cada
tick. Un `restart` o un crash no pierde posiciones. Haz backup antes de
actualizar.

**No se cae por un error.** Si un ciclo falla, se loguea y continúa al
siguiente tick. Si el proceso muere, systemd lo reinicia a los 30s. Si
crashea 5 veces en 10 minutos, systemd lo deja parado para que investigues
(`systemctl reset-failed polymarket-bot` para reactivar).

**Cierre limpio.** El bot atrapa `SIGTERM`, así que `systemctl stop` guarda
el estado antes de salir.

**Cero costos de API.** Con `FREE_ONLY=true` las dos rutas que gastan
Anthropic quedan forzadas a apagado. Solo se usan APIs públicas gratis de
Polymarket. No necesitas `ANTHROPIC_API_KEY`.

**Convivencia con otros bots.** El keep-alive HTTP está apagado por defecto
(`KEEPALIVE_ENABLED=false`), así que no ocupa el puerto 8080 ni expone nada.

## Si algo falla

```bash
journalctl -u polymarket-bot -n 100 --no-pager    # últimos 100 logs
```

- `CLOB no inicializado` → revisa `POLYMARKET_PRIVATE_KEY` en el `.env`
- `Estrategias activas: NINGUNA` → prende alguna en el `.env`
- Arranca y se para de inmediato → `systemctl status` muestra el error;
  suele ser ruta mala en `ExecStart` o permisos del `.env`
