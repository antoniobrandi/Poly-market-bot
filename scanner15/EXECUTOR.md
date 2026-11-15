# executor.py — ejecutor de señales del Scanner 15m

Toma las señales que el scanner escribe en `senales.csv` y las ejecuta en
Polymarket con la cuenta del bot de deportes. **Por default es DRY_RUN**: no
manda ninguna orden real. Solo opera con dinero si lo corres con `--live`.

## Instalación (una vez)

```bash
cp ~/Desktop/Poly-market-bot/scanner15/executor.py ~/Downloads/scanner15/
cd ~/Desktop/Poly-market-bot && git pull        # trae clob_conn.py
cd ~/Downloads/scanner15 && source .venv/bin/activate
pip install "py-clob-client-v2==1.0.0" python-dotenv requests
```

Las llaves **no se copian**: el ejecutor lee `~/Desktop/Poly-market-bot/.env`
en su lugar (si el bot está en otra carpeta: `export POLY_BOT_DIR=/ruta`).
En DRY_RUN ni siquiera abre la llave privada: solo lee orderbooks públicos.

## Uso

En una terminal, el scanner como siempre. En otra:

```bash
python executor.py --follow senales.csv          # DRY_RUN (seguro)
python executor.py --signal btc UP 0.52 0.04     # probar una señal a mano
python executor.py --status                      # resumen del día
python executor.py --check                       # probar llaves; NO opera
python executor.py --follow senales.csv --live   # REAL — solo cuando decidas
```

Al arrancar `--follow` imprime qué columna del CSV usa para cada dato. Si
adivina mal, fuérzalas: `--col side=senal --col price=ask --col edge=edge_neto`.

## Reglas (fijas en el código, arriba de executor.py)

| Regla | Cómo se cumple |
|---|---|
| DRY_RUN por default | Sin `--live` usa un cliente sin llave y no crea/firma órdenes. Guarda en `ordenes_dry.csv` |
| $1 por señal | `acciones = $1 / precio`, redondeado hacia arriba al centavo (≈$1.00–1.01) |
| Igual que el papel | Cada fila nueva de `senales.csv` es una orden, al instante y en el orden en que llegó. Una sola por mercado (moneda + ventana), como `paper.has(slug)`. Si varias monedas marcan señal en la misma ventana, entra en todas (mientras no pase el límite diario). Una vez enviada, ese mercado queda usado aunque no se llene |
| Orden límite al ask | GTC al precio del ask de la señal (redondeado hacia abajo al tick). Nunca a mercado |
| Cancelar a los 10 s | Si no se llenó toda, cancela el resto y registra lo que se llenó (PARCIAL) |
| Pérdida diaria $5 | Cuenta lo perdido hoy **más** lo abierto como si valiera 0. Si la siguiente orden pudiera pasar de $5, no la manda: espera a que resuelvan las abiertas, o hasta mañana si ya no hay abiertas |
| Archivo `STOP` | `touch STOP` → no opera, y si hay una orden esperando se cancela ya. `rm STOP` para reanudar |
| `ordenes.csv` | precio de la señal, precio llenado, costo, comisión estimada, resultado, pago y P&L |

Además re-chequea lo mismo que el scanner (30–70¢, edge ≥3¢, quedan >2 min) y
descarta señales con más de 30 s de antigüedad.

## Ojo

- **Mínimo de acciones**: muchos mercados de Polymarket piden 5 acciones por
  orden. Con $1 a 50¢ son 2 acciones. Si el mercado lo rechaza, el ejecutor
  avisa "el mercado pide mínimo …" y no opera (no sube el monto solo).
- **Comisión**: se estima con `0.07 × p × (1−p)` por acción y se resta del P&L.
  Por eso cada pérdida cuenta ≈$1.03, y caben 4 órdenes abiertas a la vez
  dentro de $5. Con 7 señales en la misma ventana entra en las primeras 4.
- **Cuenta compartida**: puedes apostar a mano en la misma cuenta. El ejecutor
  solo cancela SUS órdenes (por ID, nunca "cancelar todo") y el límite de $5
  cuenta solo sus órdenes, no tus apuestas manuales. Si tus apuestas dejan sin
  saldo la cuenta, Polymarket rechaza la orden y queda como RECHAZADA.
- **Cobrar ganancias**: el ejecutor no redime posiciones ganadoras; eso se
  hace en Polymarket como siempre.
- El resultado se lee de Gamma cuando el mercado cierra (unos minutos tras la
  ventana). Si apagas el ejecutor, al volver a prenderlo liquida lo pendiente.

Pruebas (sin red ni dinero): `python -m pytest -q test_executor.py`
