# Scanner 15m 24/7 en Railway

Corre en un servicio **aparte** del bot de deportes, en el mismo repo:

- `scanner.py --headless`: escanea, registra señales y papel, y guarda una foto
  del mercado por minuto (`mercado.csv`) más el ganador de cada ventana
  (`resultados.csv`).
- `executor.py --follow`: en **DRY_RUN** salvo que pongas `EXECUTOR_LIVE=true`.
- `webstatus.py`: página con contraseña para ver el estado desde el cel y bajar
  los archivos.

## Configuración (una vez)

1. En Railway, abre tu proyecto → **+ New → GitHub Repo → Poly-market-bot**.
2. En el servicio nuevo → **Settings**:
   - **Source → Branch**: `claude/vibrant-hopper-9ipyjk` (o `main` cuando se
     haga merge del PR).
   - **Config-as-code → Railway config file**: `scanner15/railway.json`.
     Así usa `scanner15/Dockerfile` y no el del bot de deportes.
   - **Deploy → Region**: una de **Europa** (p. ej. EU West / Amsterdam).
     **Obligatorio**: Binance bloquea servidores de EE. UU.
   - **Networking → Generate Domain**: para la página de estado.
3. **+ New → Volume**, conectado a este servicio, con mount path **`/data`**.
   Ahí quedan papel, señales y registros aunque el servicio se reinicie.
4. **Variables**:

   | Variable | Valor | Para qué |
   |---|---|---|
   | `DATA_TOKEN` | una contraseña larga inventada | abre la página de estado |
   | `SCANNER_COINS` | (opcional) `btc eth` | limitar monedas; vacío = las 7 |
   | `EXECUTOR_LIVE` | **no la pongas** | sin ella, el ejecutor solo simula |

   **No pongas tu llave privada.** En DRY_RUN no hace falta.
5. **Deploy**. En **Logs** debe salir:
   - `🧪 Ejecutor en DRY_RUN`
   - `Scanner 15m (sin pantalla) · modelo v2 ...`
   - cada 5 min, una línea `papel v2: ...`

## Verlo desde el cel

```
https://<tu-dominio>.up.railway.app/?t=<DATA_TOKEN>
```

Descargar un archivo (para el análisis):

```
https://<tu-dominio>.up.railway.app/f/papel.json?t=<DATA_TOKEN>
https://<tu-dominio>.up.railway.app/f/mercado.csv?t=<DATA_TOKEN>
https://<tu-dominio>.up.railway.app/f/resultados.csv?t=<DATA_TOKEN>
```

## Dinero real (más adelante, solo si el papel sale positivo)

Requiere poner la llave como variables secretas de Railway
(`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_PROXY_ADDRESS` y las tres `POLYMARKET_API_*`)
y `EXECUTOR_LIVE=true`. Es la parte delicada: la llave quedaría en el servidor.
Para apagarlo: borra `EXECUTOR_LIVE` (o ponla en `false`) y Railway reinicia en
DRY_RUN.

## Notas

- Horarios y "día" del límite diario en hora de Ciudad de México.
- No corras el ejecutor en real en la Mac y en Railway al mismo tiempo: serían
  órdenes dobles.
