#!/usr/bin/env python3
"""
executor.py — ejecuta en Polymarket las señales del Scanner 15m.

Seguridad primero:
  · DRY_RUN por default. Solo opera con dinero real si se corre con --live.
  · $1 por señal, igual que el registro en papel del scanner: cada señal de
    senales.csv es una orden, en el momento, una sola por mercado (moneda+ventana).
  · Compra por MONTO ($1), con precio tope = ask de la señal: nunca paga más.
    Es FAK (llena lo que haya a ese precio, no queda en el libro). Si no hay
    nada al ask, reintenta cada segundo hasta 10 s y luego se rinde.
    Así lo compra la web de Polymarket; una orden que queda en el libro
    exige mínimo 5 acciones y con $1 no alcanza.
  · Pérdida diaria máxima $5: al llegar, no opera hasta el día siguiente.
  · Si existe el archivo STOP en esta carpeta, no opera.
  · Cada orden real queda en ordenes.csv (las de DRY_RUN en ordenes_dry.csv).

La firma y la conexión al CLOB V2 se reusan del bot de deportes
(~/Desktop/Poly-market-bot/clob_conn.py). Las llaves se leen de SU .env, en
su lugar: no se copian, no se exportan y no se imprimen.

Uso típico (en otra terminal, junto al scanner):
    python executor.py --follow senales.csv        # DRY_RUN
    python executor.py --signal btc UP 0.52 0.04   # probar una señal a mano
    python executor.py --check                     # probar llaves (no opera)
    python executor.py --status                    # resumen del día
    python executor.py --follow senales.csv --live # REAL (lo decides tú)
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import socket
import sys
import time
import unicodedata
from datetime import datetime
from typing import Callable, Optional

# ═══════════════════════════════════════════════════════════
#  REGLAS — cambiarlas exige editar este archivo a propósito
# ═══════════════════════════════════════════════════════════
USD_PER_SIGNAL   = 1.00   # dólares por señal
FILL_TIMEOUT_S   = 10     # reintenta hasta 10 s si no hay nada al ask; después se rinde
MAX_DAILY_LOSS   = 5.00   # pérdida diaria (USD) ya cerrada; al llegar, no opera hasta mañana
MIN_PRICE        = 0.30   # mismo rango que el scanner
MAX_PRICE        = 0.70
MIN_EDGE         = 0.03   # edge neto mínimo (después de comisión)
MIN_SECONDS_LEFT = 120    # quedan más de 2 minutos
MAX_SIGNAL_AGE_S = 30     # una señal más vieja que esto ya no es la misma apuesta
TAKER_FEE_RATE   = 0.07   # comisión estimada = acciones × rate × p × (1−p)
WINDOW_S         = 900

HERE    = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.expanduser(os.getenv("POLY_BOT_DIR", "~/Desktop/Poly-market-bot"))
GAMMA   = "https://gamma-api.polymarket.com"

log = logging.getLogger("executor")


# ═══════════════════════════════════════════════════════════
#  Utilidades
# ═══════════════════════════════════════════════════════════
def force_ipv4() -> None:
    """Mismo arreglo que el scanner: IPv6 hacía muy lentas las peticiones."""
    try:
        import urllib3.util.connection as uc
        uc.allowed_gai_family = lambda: socket.AF_INET
    except Exception:
        pass
    try:  # cliente HTTP del CLOB (httpx): atar a 0.0.0.0 fuerza IPv4
        import httpx
        from py_clob_client_v2.http_helpers import helpers as h
        h._http_client = httpx.Client(
            http2=True, transport=httpx.HTTPTransport(local_address="0.0.0.0", http2=True))
    except Exception:
        pass


def floor_to(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step



def fee_estimate(shares: float, price: float) -> float:
    return shares * TAKER_FEE_RATE * price * (1 - price)


def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def parse_num(v) -> Optional[float]:
    """'52.3¢' / '0.523' / '3.1%' → fracción (0.523, 0.031). ≥1 se lee como centavos."""
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    if not s:
        return None
    m = re.search(r"[-+]?\d*\.?\d+", s)
    if not m:
        return None
    x = float(m.group())
    if "¢" in s or "%" in s or abs(x) >= 1:
        x /= 100.0
    return x


def parse_side(v) -> Optional[str]:
    s = _norm(v).upper()
    if not s:
        return None
    if "UP" in s or "SUB" in s or "ARRIBA" in s:
        return "UP"
    if "DOWN" in s or "BAJ" in s or "ABAJO" in s:
        return "DOWN"
    return None


def parse_ts(v) -> Optional[float]:
    s = str(v or "").strip()
    if not s:
        return None
    try:
        x = float(s)
        return x / 1000.0 if x > 1e12 else (x if x > 1e9 else None)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()  # sin zona → hora local de la Mac
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:  # solo hora → hoy
        now = datetime.now()
        dt = now.replace(hour=int(m[1]), minute=int(m[2]), second=int(m[3] or 0), microsecond=0)
        return dt.timestamp()
    return None


SLUG_RE = re.compile(r"([a-z]+)-updown-15m-(\d{9,11})")


# ═══════════════════════════════════════════════════════════
#  Señal
# ═══════════════════════════════════════════════════════════
class Signal:
    def __init__(self, coin: str, side: str, price: float, edge: float,
                 window_start: Optional[int] = None, ts: Optional[float] = None):
        self.ts = ts if ts is not None else time.time()
        self.coin = coin.lower().strip()
        self.side = side.upper()
        self.price = float(price)
        self.edge = float(edge)
        self.window_start = int(window_start if window_start is not None
                                else self.ts // WINDOW_S * WINDOW_S)

    @property
    def slug(self) -> str:
        return f"{self.coin}-updown-15m-{self.window_start}"

    @property
    def window_end(self) -> int:
        return self.window_start + WINDOW_S

    def __repr__(self) -> str:
        return (f"{self.coin.upper()} {self.side} @{self.price*100:.1f}¢ "
                f"edge {self.edge*100:+.1f}¢")


# ═══════════════════════════════════════════════════════════
#  Gamma (mercados) — inyectable en pruebas
# ═══════════════════════════════════════════════════════════
class Gamma:
    def __init__(self):
        import requests
        self.s = requests.Session()

    def market(self, slug: str) -> Optional[dict]:
        r = self.s.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        d = r.json()
        if isinstance(d, list) and d:
            return d[0]
        # respaldo: el mismo camino que usa scanner.py (/events?slug=…)
        r = self.s.get(f"{GAMMA}/events", params={"slug": slug}, timeout=8)
        r.raise_for_status()
        ev = r.json()
        if isinstance(ev, list) and ev and ev[0].get("markets"):
            return ev[0]["markets"][0]
        return None


def _jlist(v) -> list:
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except Exception:
        return []


def token_for_side(market: dict, side: str) -> Optional[str]:
    outs = [str(o).strip().lower() for o in _jlist(market.get("outcomes"))]
    toks = _jlist(market.get("clobTokenIds"))
    want = "up" if side == "UP" else "down"
    if want in outs and len(toks) == len(outs):
        return str(toks[outs.index(want)])
    return None


def winner_of(market: dict) -> Optional[str]:
    """'UP'/'DOWN' si el mercado ya resolvió; None si no."""
    if not market or not market.get("closed"):
        return None
    outs = [str(o).strip().upper() for o in _jlist(market.get("outcomes"))]
    prices = [float(p) for p in _jlist(market.get("outcomePrices"))]
    for o, p in zip(outs, prices):
        if p >= 0.99:
            return o
    return None


# ═══════════════════════════════════════════════════════════
#  Ejecutor
# ═══════════════════════════════════════════════════════════
CSV_COLS = [
    "fecha", "modo", "ventana_inicio", "moneda", "lado", "slug", "order_id",
    "precio_senal", "edge_senal", "precio_limite", "acciones_pedidas",
    "acciones_llenadas", "precio_llenado", "costo_usd", "comision_est_usd",
    "estado", "resultado", "pago_usd", "pnl_usd",
]


class Executor:
    def __init__(self, live: bool = False, clob=None, gamma=None, base_dir: str = HERE,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 fill_timeout: float = FILL_TIMEOUT_S, poll_s: float = 1.0):
        self.live = bool(live)
        self.mode = "LIVE" if self.live else "DRY_RUN"
        self.base_dir = base_dir
        self.now, self.sleep = now, sleep
        self.fill_timeout, self.poll_s = fill_timeout, poll_s
        sfx = "" if self.live else "_dry"
        self.state_path = os.path.join(base_dir, f"executor_state{sfx}.json")
        self.csv_path = os.path.join(base_dir, f"ordenes{sfx}.csv")
        self.stop_path = os.path.join(base_dir, "STOP")
        self.gamma = gamma or Gamma()
        self._clob = clob
        self.state = self._load_state()
        self._retries: list[dict] = []   # señales sin llenar que se reintentan en paralelo
        for o in self.state["orders"]:
            if o.get("estado") in ("ENVIANDO", "REINTENTANDO"):
                o.update(estado="INTERRUMPIDA", resultado="—", pnl_usd=0)
                log.warning(f"⚠️  {o['slug']}: el ejecutor se cortó mientras compraba — "
                            f"revisa en Polymarket si se llenó")

    # ── conexión ─────────────────────────────────────────────
    @property
    def clob(self):
        if self._clob is None:
            sys.path.insert(0, BOT_DIR)
            try:
                import clob_conn
            except ImportError as e:
                raise RuntimeError(
                    f"No encuentro clob_conn.py en {BOT_DIR} (haz git pull del bot "
                    f"o define POLY_BOT_DIR): {e}")
            if self.live:
                env = clob_conn.load_bot_env(BOT_DIR)
                self._clob = clob_conn.make_trading_client(env=env)
                log.info(f"🔑 CLOB autenticado: {clob_conn.describe_wallet(env)}")
            else:
                self._clob = clob_conn.make_public_client()  # sin llave
        return self._clob

    # ── estado ───────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            st.setdefault("markets", {})
            st.setdefault("orders", [])
            return st
        except FileNotFoundError:
            return {"markets": {}, "orders": []}

    def _save(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)
        self._write_csv()

    def _write_csv(self) -> None:
        tmp = self.csv_path + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
            w.writeheader()
            for o in self.state["orders"]:
                w.writerow(o)
        os.replace(tmp, self.csv_path)

    # ── riesgo ───────────────────────────────────────────────
    def realized_today(self) -> float:
        today = local_day(self.now())
        return sum(float(o.get("pnl_usd") or 0) for o in self.state["orders"]
                   if o.get("fecha", "")[:10] == today and o.get("resultado") in ("GANA", "PIERDE"))

    def open_risk(self) -> float:
        """Peor caso de lo abierto: todo lo llenado y sin resolver puede valer 0."""
        today = local_day(self.now())
        return sum(float(o["costo_usd"]) + float(o["comision_est_usd"])
                   for o in self.state["orders"]
                   if o.get("resultado") == "PENDIENTE" and o.get("fecha", "")[:10] == today)

    def blocked_reason(self, new_risk: float = 0.0) -> Optional[str]:
        if os.path.exists(self.stop_path):
            return "existe el archivo STOP"
        real = self.realized_today()
        if real <= -MAX_DAILY_LOSS + 1e-9:
            return f"límite diario alcanzado ({real:+.2f} USD) — vuelve mañana"
        return None

    # ── entrada principal ────────────────────────────────────
    def handle_signals(self, signals: list[Signal], wait: bool = True) -> list[dict]:
        """Igual que el papel del scanner: cada señal válida es una orden, en el
        orden en que llegó, y solo una por mercado (moneda + ventana).
        El primer intento de TODAS sale al instante; las que no llenan se
        reintentan en paralelo, cada una con sus propios 10 s. Con wait=False
        (modo --follow) los reintentos avanzan con retry_pending()."""
        now = self.now()
        done = []
        for s in sorted(signals, key=lambda x: x.ts):
            why = self._reject_reason(s, now)
            if why:
                log.info(f"   ✗ {s}: {why}")
                continue
            if s.slug in self.state["markets"]:
                log.info(f"   ⏭  {s.slug} ya tiene orden — ignoro")
                continue
            rec = self.place(s)
            if rec:
                done.append(rec)
        while wait and self._retries:
            self.sleep(self.poll_s)
            self.retry_pending()
        return done

    def _reject_reason(self, s: Signal, now: float) -> Optional[str]:
        if s.side not in ("UP", "DOWN"):
            return "lado inválido"
        if not (MIN_PRICE <= s.price <= MAX_PRICE):
            return f"precio fuera de {MIN_PRICE*100:.0f}–{MAX_PRICE*100:.0f}¢"
        if s.edge < MIN_EDGE - 1e-9:
            return f"edge < {MIN_EDGE*100:.0f}¢"
        if s.window_end - now <= MIN_SECONDS_LEFT:
            return "quedan 2 min o menos"
        if now - s.ts > MAX_SIGNAL_AGE_S:
            return f"señal vieja ({now - s.ts:.0f}s)"
        return None

    @staticmethod
    def _hhmm(ws: int) -> str:
        return datetime.fromtimestamp(ws).strftime("%H:%M")

    # ── colocar una orden ────────────────────────────────────
    def place(self, s: Signal) -> Optional[dict]:
        block = self.blocked_reason()
        if block:
            log.warning(f"⛔ No opero {s}: {block}")
            return None

        market, err = None, None
        for intento in range(3):            # la red de casa a veces corta la conexión
            try:
                market, err = self.gamma.market(s.slug), None
                break
            except Exception as e:
                err = e
                self.sleep(0.5)
        if err is not None:
            log.error(f"   No pude leer el mercado {s.slug} (3 intentos): {err}")
            return None
        if not market or market.get("closed") or market.get("acceptingOrders") is False:
            log.warning(f"   Mercado {s.slug} no disponible / no acepta órdenes")
            return None
        token = token_for_side(market, s.side)
        if not token:
            log.error(f"   No encontré el token {s.side} en {s.slug}")
            return None

        book, err = None, None
        for intento in range(3):
            try:
                book, err = self.clob.get_order_book(token), None
                break
            except Exception as e:
                err = e
                self.sleep(0.5)
        if err is not None:
            log.error(f"   No pude leer el orderbook (3 intentos): {err}")
            return None
        tick = str(book.get("tick_size") or market.get("orderPriceMinTickSize") or "0.01")
        tick_f = float(tick)
        neg_risk = bool(book.get("neg_risk") if book.get("neg_risk") is not None
                        else market.get("negRisk", False))

        limit = round(floor_to(s.price, tick_f), 6)  # tope: nunca pagar más que el ask de la señal
        shares = round(USD_PER_SIGNAL / limit, 4)    # referencia; se compra por monto ($1)

        # Desde aquí el mercado queda USADO (aunque la orden no se llene o falle):
        # una sola orden por mercado, sin reintentos — igual que el papel.
        rec = {
            "fecha": datetime.fromtimestamp(self.now()).strftime("%Y-%m-%d %H:%M:%S"),
            "modo": self.mode, "ventana_inicio": s.window_start,
            "moneda": s.coin.upper(), "lado": s.side, "slug": s.slug, "order_id": "",
            "precio_senal": round(s.price, 4), "edge_senal": round(s.edge, 4),
            "precio_limite": limit, "acciones_pedidas": shares,
            "acciones_llenadas": 0, "precio_llenado": "", "costo_usd": 0,
            "comision_est_usd": 0, "estado": "ENVIANDO", "resultado": "",
            "pago_usd": "", "pnl_usd": "", "_token": token,
        }
        self.state["markets"][s.slug] = rec["fecha"]
        self.state["orders"].append(rec)
        self._save()

        log.info(f"{'🧪' if not self.live else '💸'} [{self.mode}] BUY {s.coin.upper()} {s.side} "
                 f"${USD_PER_SIGNAL:.2f} tope {limit*100:.1f}¢ (≈{shares:.2f} acc) | {s.slug}")
        job = {"rec": rec, "token": token, "limit": limit, "tick": tick,
               "neg_risk": neg_risk, "t0": self.now(), "tries": 0}
        if self._attempt(job) == "nofill":
            rec["estado"] = "REINTENTANDO"
            self._retries.append(job)
            self._save()
        return rec

    def retry_pending(self) -> None:
        """Un reintento para cada señal que aún no llena; cierra las que pasan 10 s."""
        for job in list(self._retries):
            if self.now() - job["t0"] >= self.fill_timeout or os.path.exists(self.stop_path):
                job["rec"]["estado"] = "SIN_LLENAR"
                why = "STOP" if os.path.exists(self.stop_path) else f"{self.fill_timeout:g}s"
                log.info(f"   ⌛ {job['rec']['moneda']} {job['rec']['lado']}: {job['tries']} "
                         f"intento(s), {why} sin nada a ≤{job['limit']*100:.1f}¢ → no compra")
                self._retries.remove(job)
                self._finalize(job["rec"])
            elif self._attempt(job) != "nofill":
                self._retries.remove(job)

    def _attempt(self, job: dict) -> str:
        """Un intento. Devuelve 'filled', 'rejected' o 'nofill'. Cierra el registro si no es nofill."""
        rec = job["rec"]
        job["tries"] += 1
        try:
            r = (self._try_live if self.live else self._try_dry)(job)
        except Exception as e:
            rec["estado"] = "ERROR"
            log.error(f"   ❌ Error con la orden {rec['slug']}: {e}")
            r = "rejected"
        if r != "nofill":
            self._finalize(rec)
        return r

    def _finalize(self, rec: dict) -> None:
        filled = float(rec["acciones_llenadas"] or 0)
        if filled > 0:
            rec["resultado"] = "PENDIENTE"
        else:
            rec["resultado"] = "—"
            rec["pnl_usd"] = 0
        self._save()
        log.info(f"   → {rec['moneda']} {rec['lado']} {rec['estado']}: {filled:g} acc"
                 + (f" @ {float(rec['precio_llenado'])*100:.1f}¢, costo ${float(rec['costo_usd']):.2f}"
                    if filled else ""))

    def _fill(self, rec: dict, shares: float, cost: float, taker_shares: float, price: float):
        rec["acciones_llenadas"] = round(shares, 4)
        rec["costo_usd"] = round(cost, 4)
        rec["precio_llenado"] = round(cost / shares, 4) if shares > 0 else ""
        rec["comision_est_usd"] = round(fee_estimate(taker_shares, price), 4)

    def _try_dry(self, job: dict) -> str:
        """Simula la FAK: compra con $1 lo que haya en el libro a ≤ tope."""
        rec, limit = job["rec"], job["limit"]
        rec["order_id"] = rec["order_id"] or f"dry-{int(job['t0'])}"
        book = self.clob.get_order_book(job["token"])
        levels = sorted((float(a["price"]), float(a["size"])) for a in (book.get("asks") or []))
        usd_left, sh, cost = USD_PER_SIGNAL, 0.0, 0.0
        for px, size in levels:
            if px > limit + 1e-9 or usd_left <= 1e-9:
                break
            take = min(size, usd_left / px)
            sh, cost, usd_left = sh + take, cost + take * px, usd_left - take * px
        if sh <= 0:
            return "nofill"
        self._fill(rec, sh, cost, sh, cost / sh)
        rec["estado"] = "LLENADA" if usd_left <= 0.01 else "PARCIAL"
        return "filled"

    _NO_FILL = ("no orders found", "no match", "fully filled", "fok", "fak")

    def _try_live(self, job: dict) -> str:
        """$1 por monto, tope = ask de la señal, FAK. Nunca deja una orden viva."""
        from py_clob_client_v2.clob_types import (
            MarketOrderArgs, OrderType, PartialCreateOrderOptions)
        rec, limit, tick = job["rec"], job["limit"], job["tick"]
        resp, err = None, ""
        try:
            order = self.clob.create_market_order(
                MarketOrderArgs(token_id=job["token"], amount=USD_PER_SIGNAL, side="BUY",
                                price=limit, order_type=OrderType.FAK),
                PartialCreateOrderOptions(tick_size=tick, neg_risk=job["neg_risk"]),
            )
            resp = self.clob.post_order(order, OrderType.FAK)
        except Exception as e:
            err = str(e)
        if isinstance(resp, dict):
            err = err or str(resp.get("errorMsg") or "")
            if resp.get("orderID"):
                rec["order_id"] = resp["orderID"]

        if isinstance(resp, dict) and resp.get("success", True) is not False \
                and str(resp.get("status", "")).lower() == "matched":
            try:
                cost = float(resp.get("makingAmount") or 0)
                sh = float(resp.get("takingAmount") or 0)
            except (TypeError, ValueError):
                cost = sh = 0.0
            if sh > 1000:                      # vino en unidades de 6 decimales
                sh, cost = sh / 1e6, cost / 1e6
            if sh <= 0 or cost <= 0 or cost / sh > limit + float(tick) + 1e-9:
                cost, sh = USD_PER_SIGNAL, USD_PER_SIGNAL / limit   # supuesto conservador
            self._fill(rec, sh, cost, sh, cost / sh)
            rec["estado"] = "LLENADA" if cost >= USD_PER_SIGNAL - 0.01 else "PARCIAL"
            return "filled"

        if (not err) or any(k in err.lower() for k in self._NO_FILL):
            return "nofill"
        rec["estado"] = "RECHAZADA"
        log.error(f"   ❌ Polymarket rechazó la orden {rec['slug']}: {err[:200]}")
        return "rejected"

    # ── liquidación ──────────────────────────────────────────
    def settle_pending(self) -> int:
        n = 0
        now = self.now()
        for o in self.state["orders"]:
            if o.get("resultado") != "PENDIENTE":
                continue
            if now < int(o["ventana_inicio"]) + WINDOW_S + 20:
                continue
            if now < float(o.get("_next_check", 0)):
                continue
            o["_next_check"] = now + 30
            try:
                win = winner_of(self.gamma.market(o["slug"]))
            except Exception as e:
                log.warning(f"   No pude revisar {o['slug']}: {e}")
                continue
            if not win:
                continue
            shares = float(o["acciones_llenadas"])
            pago = shares if win == o["lado"] else 0.0
            pnl = pago - float(o["costo_usd"]) - float(o["comision_est_usd"])
            o.update(resultado="GANA" if pago else "PIERDE",
                     pago_usd=round(pago, 4), pnl_usd=round(pnl, 4))
            n += 1
            log.info(f"{'✅' if pago else '❌'} {o['moneda']} {o['lado']} "
                     f"({self._hhmm(int(o['ventana_inicio']))}) salió {win}: P&L {pnl:+.2f} USD "
                     f"| hoy {self.realized_today():+.2f}")
        if n:
            self._save()
        return n

    def status(self) -> str:
        today = local_day(self.now())
        hoy = [o for o in self.state["orders"] if o.get("fecha", "")[:10] == today]
        res = [o for o in self.state["orders"] if o.get("resultado") in ("GANA", "PIERDE")]
        g = sum(1 for o in res if o["resultado"] == "GANA")
        tot = sum(float(o["pnl_usd"]) for o in res)
        block = self.blocked_reason()
        return (f"[{self.mode}] hoy: {len(hoy)} órdenes, P&L {self.realized_today():+.2f} "
                f"(abierto {self.open_risk():.2f}) | total: {g}/{len(res)} aciertos, "
                f"P&L {tot:+.2f}" + (f" | ⛔ {block}" if block else " | ✅ puede operar"))


# ═══════════════════════════════════════════════════════════
#  Seguir senales.csv
# ═══════════════════════════════════════════════════════════
COLUMN_HINTS = {
    "coin":   ["moneda", "coin", "cripto", "crypto", "asset", "activo", "symbol", "simbolo"],
    "side":   ["senal", "signal", "lado", "side", "direccion", "direction"],
    "price":  ["ask", "precio_ask", "precio", "price", "entrada", "entry"],
    "edge":   ["edge_neto", "edge", "ventaja"],
    "window": ["ventana", "window_start", "window", "inicio", "slug", "mercado", "market"],
    "ts":     ["timestamp", "ts", "fecha_hora", "datetime", "time", "hora", "fecha"],
}


def map_columns(header: list[str], overrides: dict) -> dict:
    normed = {_norm(h): h for h in header}
    out = {}
    # El scanner escribe up_ask y down_ask; "precio" ahí es el spot de la cripto.
    for side in ("up", "down"):
        hit = next((o for n, o in normed.items() if "ask" in n and side in n.split("_")), None)
        if hit:
            out[f"ask_{side}"] = hit
    for key, hints in COLUMN_HINTS.items():
        if key in overrides:
            out[key] = overrides[key]
            continue
        if key == "price" and "ask_up" in out and "ask_down" in out:
            continue
        for h in hints:                       # nombre exacto primero
            if h in normed:
                out[key] = normed[h]
                break
        else:
            for h in hints:                   # luego "contiene"
                hit = next((orig for n, orig in normed.items() if h in n), None)
                if hit and hit not in out.values():
                    out[key] = hit
                    break
    return out


def row_to_signal(row: dict, cols: dict, arrival: float) -> Optional[Signal]:
    side = parse_side(row.get(cols.get("side", ""), ""))
    if not side:
        return None                           # fila sin señal (vacía / "—")
    coin = window = None
    raw_win = str(row.get(cols.get("window", ""), "") or "")
    m = SLUG_RE.search(raw_win.lower())
    if m:
        coin, window = m[1], int(m[2])
    elif raw_win:
        t = parse_ts(raw_win)
        window = int(t // WINDOW_S * WINDOW_S) if t else None
    coin = coin or re.sub(r"(usdt|usdc|usd)$", "",
                          _norm(row.get(cols.get("coin", ""), "")).split("_")[0])
    if "price" in cols:
        price = parse_num(row.get(cols["price"]))
    else:
        price = parse_num(row.get(cols.get("ask_up" if side == "UP" else "ask_down", "")))
    edge = parse_num(row.get(cols.get("edge", "")))
    if not coin or price is None or edge is None:
        return None
    ts = parse_ts(row.get(cols.get("ts", ""))) or arrival
    return Signal(coin, side, price, edge, window_start=window, ts=ts)


class CsvFollower:
    """Lee solo las filas nuevas que el scanner agrega a senales.csv."""

    def __init__(self, path: str, overrides: dict, from_start: bool = False):
        self.path, self.overrides = path, overrides
        self.header: Optional[list[str]] = None
        self.cols: dict = {}
        self.pos = 0
        self.from_start = from_start
        self._warned_missing = False

    def _open_header(self) -> bool:
        if not os.path.exists(self.path):
            if not self._warned_missing:
                log.info(f"📄 {self.path} aún no existe — espero a que el scanner escriba "
                         f"la primera señal")
                self._warned_missing = True
            return False
        if self._warned_missing:   # se creó después de arrancar: todo lo que trae es nuevo
            self.from_start, self._warned_missing = True, False
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            first = f.readline()
            if not first.endswith("\n"):
                return False
            self.header = next(csv.reader([first]))
            self.pos = f.tell() if self.from_start else os.path.getsize(self.path)
        self.cols = map_columns(self.header, self.overrides)
        need = [k for k in ("side", "price", "edge") if k not in self.cols]
        if "price" in need and "ask_up" in self.cols and "ask_down" in self.cols:
            need.remove("price")
        if "coin" not in self.cols and "window" not in self.cols:
            need.append("coin")
        log.info(f"📄 {self.path}: columnas {self.header}")
        log.info(f"   mapeo detectado: {self.cols}")
        if need:
            raise SystemExit(
                f"No identifico las columnas {need} en {self.path}.\n"
                f"Indícalas a mano, p. ej.: --col side=senal --col price=ask_lado")
        return True

    def poll(self) -> list[Signal]:
        if self.header is None and not self._open_header():
            return []
        size = os.path.getsize(self.path)
        if size < self.pos:                   # archivo truncado / recreado
            log.info("📄 senales.csv cambió de tamaño: releo encabezado")
            self.header, self.from_start = None, True
            return []
        out = []
        with open(self.path, newline="", encoding="utf-8") as f:
            f.seek(self.pos)
            chunk = f.read()
        if not chunk:
            return []
        last_nl = chunk.rfind("\n")
        if last_nl < 0:
            return []                         # línea a medio escribir
        complete = chunk[: last_nl + 1]
        self.pos += len(complete.encode("utf-8"))
        arrival = time.time()
        for row in csv.DictReader(complete.splitlines(), fieldnames=self.header):
            try:
                s = row_to_signal(row, self.cols, arrival)
            except Exception as e:
                log.warning(f"   fila ilegible ({e}): {row}")
                continue
            if s:
                out.append(s)
        return out


def follow(ex: Executor, path: str, overrides: dict, from_start: bool = False) -> None:
    fol = CsvFollower(path, overrides, from_start)
    last_status = 0.0
    log.info(f"👀 Siguiendo {path} — Ctrl+C para salir. {ex.status()}")
    while True:
        try:
            new = fol.poll()
            if new:
                log.info(f"📥 {len(new)} señal(es): {new}")
                ex.handle_signals(new, wait=False)   # al instante, como el papel
            ex.retry_pending()                       # reintentos en paralelo (10 s c/u)
            ex.settle_pending()
            if time.time() - last_status > 300:
                log.info(ex.status())
                last_status = time.time()
        except SystemExit:
            raise
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error(f"Error en el ciclo: {e}")
        time.sleep(1)


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════
class _HideFakNoMatch(logging.Filter):
    """La librería imprime un error por cada FAK sin contraparte; para nosotros
    es un reintento normal (el ejecutor ya avisa SIN_LLENAR al final)."""
    def filter(self, record):
        return "no orders found to match" not in record.getMessage()


def setup_logging(base_dir: str) -> None:
    logging.getLogger("py_clob_client_v2.http_helpers.helpers").addFilter(_HideFakNoMatch())
    fmt = logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    for h in (logging.StreamHandler(sys.stdout),
              logging.FileHandler(os.path.join(base_dir, "executor.log"))):
        h.setFormatter(fmt)
        log.addHandler(h)


def live_banner() -> None:
    print("\n" + "═" * 60)
    print("  💸  MODO --live: ÓRDENES REALES CON DINERO REAL")
    print(f"  ${USD_PER_SIGNAL:.2f}/señal · 1 orden por señal (como el papel) · límite diario ${MAX_DAILY_LOSS:.2f}")
    print("  Para parar: crea el archivo STOP o Ctrl+C")
    print("═" * 60)
    for i in range(5, 0, -1):
        print(f"  Empieza en {i}… (Ctrl+C para abortar)", end="\r", flush=True)
        time.sleep(1)
    print()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ejecutor de señales del Scanner 15m (DRY_RUN por default)")
    p.add_argument("--live", action="store_true", help="opera con dinero REAL")
    p.add_argument("--follow", metavar="CSV", help="seguir el CSV de señales del scanner")
    p.add_argument("--from-start", action="store_true", help="con --follow: leer también filas viejas")
    p.add_argument("--col", action="append", default=[], metavar="CLAVE=COLUMNA",
                   help="forzar columna: coin, side, price, edge, window, ts")
    p.add_argument("--signal", nargs=4, metavar=("MONEDA", "LADO", "ASK", "EDGE"),
                   help="procesar una señal a mano, p. ej. --signal btc UP 0.52 0.04")
    p.add_argument("--check", action="store_true", help="probar llaves y conexión (no opera)")
    p.add_argument("--status", action="store_true", help="resumen y salir")
    a = p.parse_args(argv)

    setup_logging(HERE)
    force_ipv4()

    if a.check:
        try:
            sys.path.insert(0, BOT_DIR)
            import clob_conn
            env = clob_conn.load_bot_env(BOT_DIR)
            c = clob_conn.make_trading_client(env=env)
            n = len(c.get_open_orders())
        except Exception as e:
            print(f"❌ No conecta: {e}")
            return 1
        print(f"✅ Conexión OK — {clob_conn.describe_wallet(env)} — órdenes abiertas: {n}")
        return 0

    ex = Executor(live=a.live)
    if a.status:
        ex.settle_pending()
        print(ex.status())
        return 0

    if a.live:
        live_banner()
        try:
            _ = ex.clob  # falla aquí (antes de cualquier señal) si las llaves no sirven
        except Exception as e:
            log.error(f"❌ No pude conectar al CLOB con las llaves del bot: {e}")
            return 1
    else:
        log.info("🧪 DRY_RUN — no se envía ninguna orden real (usa --live para operar)")

    if a.signal:
        coin, side, ask, edge = a.signal
        ex.handle_signals([Signal(coin, parse_side(side) or side, parse_num(ask), parse_num(edge))])
        print(ex.status())
        return 0

    if a.follow:
        overrides = dict(kv.split("=", 1) for kv in a.col)
        try:
            follow(ex, a.follow, overrides, a.from_start)
        except KeyboardInterrupt:
            print("\n" + ex.status())
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
