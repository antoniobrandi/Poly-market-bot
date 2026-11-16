#!/usr/bin/env python3
"""
Scanner 15m — Polymarket "Up or Down" crypto (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE)

Qué hace, cada pocos segundos:
  1. Busca el mercado de 15 min activo de cada moneda (slug <coin>-updown-15m-<inicio>).
  2. Saca el "Precio a superar" (TWAP 60s de Chainlink al inicio) de la página de Polymarket.
  3. Lee precio actual, velas de 1m/5m y orderbook (Binance; HYPE desde Hyperliquid).
  4. Calcula la probabilidad JUSTA de Up con un modelo de volatilidad que entiende
     que el cierre es un TWAP de 60 s (no un precio puntual).
  5. Compara contra lo que cobra Polymarket (best ask del CLOB) MENOS la comisión de taker.
  6. Marca señal solo si hay ventaja después de comisiones, el precio está en rango,
     queda tiempo suficiente y la tendencia de 5m no va en contra.
  7. Registra cada señal como apuesta EN PAPEL de $1 y la liquida con el resultado oficial.

Modelo v2 (corrección de fuente): el "Actual" viene de Binance en USDT, pero el
"A superar" y el cierre son de Chainlink en USD. En 228 apuestas en papel el
precio de Binance salía ~5.6 pb (0.056 %) por ENCIMA del oficial al abrir la
ventana (79 de 91 veces). Eso inflaba la probabilidad de Up (modelo 58 % vs 47 %
real). Ahora, en cada ventana se mide esa diferencia (TWAP de Binance de los
mismos 60 s contra el "A superar" oficial) y se descuenta del precio actual.

NO coloca órdenes. NO es consejo financiero. Es una herramienta para medir si existe ventaja.

Uso:
    pip install -r requirements.txt
    python scanner.py                 # todas las monedas
    python scanner.py --coins btc eth # solo algunas
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import urllib3.util.connection

# Solo IPv4: gamma-api publica 8 IPv6; si la red no enruta IPv6, urllib3 las prueba
# una por una (8 × timeout) antes de caer a IPv4 y el primer request tarda ~64 s.
urllib3.util.connection.HAS_IPV6 = False

# ───────────────────────────── CONFIG ─────────────────────────────
WINDOW = 900                 # 15 min en segundos
TWAP_LOOKBACK = 60           # el cierre es TWAP de 60 s (cryptoMarketConfig.twapLookbackSeconds)
MIN_EDGE = 0.03              # ventaja mínima por acción DESPUÉS de comisión (3¢)
PRICE_MIN, PRICE_MAX = 0.30, 0.70   # solo entra si el lado cuesta entre 30¢ y 70¢
MIN_SECONDS_LEFT = 120       # no entra en los últimos 2 min (ahí ganan los bots rápidos)
TREND_FILTER = True          # no señalar contra la tendencia de 5m
PAPER_STAKE = 1.0            # apuesta en papel por señal (USD)
REFRESH = 5                  # segundos entre actualizaciones
VOL_LOOKBACK_MIN = 120       # minutos de velas de 1m para estimar volatilidad
EWMA_LAMBDA = 0.94

DEFAULT_FEE = {"rate": 0.07, "exponent": 1}  # se sobreescribe con feeSchedule de la API

COINS = {
    # coin: (fuente, símbolo en la fuente, símbolo en Polymarket crypto-prices)
    "btc":  ("binance", "BTCUSDT", "BTC"),
    "eth":  ("binance", "ETHUSDT", "ETH"),
    "sol":  ("binance", "SOLUSDT", "SOL"),
    "xrp":  ("binance", "XRPUSDT", "XRP"),
    "doge": ("binance", "DOGEUSDT", "DOGE"),
    "bnb":  ("binance", "BNBUSDT", "BNB"),
    "hype": ("hyperliquid", "HYPE", "HYPE"),
}

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PM_WEB = "https://polymarket.com"
BINANCE = "https://api.binance.com"
HL = "https://api.hyperliquid.xyz/info"

HERE = os.path.dirname(os.path.abspath(__file__))
# En el servidor los datos van al disco persistente (SCANNER_DATA_DIR=/data)
DATA_DIR = os.getenv("SCANNER_DATA_DIR", HERE)
SIGNALS_CSV = os.path.join(DATA_DIR, "senales.csv")
PAPER_JSON = os.path.join(DATA_DIR, "papel.json")
# Registro de TODO el mercado (no solo señales): sirve para medir sin sesgo si el
# mercado o el modelo están bien calibrados. Una foto por moneda por minuto.
MARKET_CSV = os.path.join(DATA_DIR, "mercado.csv")
RESULTS_CSV = os.path.join(DATA_DIR, "resultados.csv")
SNAPSHOT_EVERY = 60

MODEL_V = 2                  # 1 = sin corrección de fuente; 2 = descuenta la base Binance↔Chainlink
MAX_BASIS = 0.003            # base mayor a 30 pb = dato raro → no se usa

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9",
})


# ───────────────────────────── MATEMÁTICAS ─────────────────────────────
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ewma_sigma_per_sec(closes: list[float], lam: float = EWMA_LAMBDA) -> Optional[float]:
    """Volatilidad EWMA de retornos log de 1 minuto, convertida a 'por segundo'."""
    if len(closes) < 20:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    var = sum(r * r for r in rets[:10]) / 10
    for r in rets[10:]:
        var = lam * var + (1 - lam) * r * r
    # piso: nunca menos que el 60% de la vol simple (evita subestimar tras minutos quietos)
    simple = sum(r * r for r in rets) / len(rets)
    var = max(var, 0.6 * simple)
    return math.sqrt(var / 60.0)


def twap_effective_seconds(secs_left: float, lookback: float = TWAP_LOOKBACK) -> float:
    """
    Varianza restante (en 'segundos de browniano') del TWAP final de `lookback` segundos.
    Si faltan τ ≥ L s:  Var = σ²(τ − 2L/3)
    Si faltan τ < L s:  Var = σ²·τ³ / (3L²)   (parte del promedio ya está fijada)
    """
    t = max(secs_left, 0.0)
    L = lookback
    if t >= L:
        return t - 2.0 * L / 3.0
    return t ** 3 / (3.0 * L * L)


def fair_prob_up(price: float, target: float, sigma_sec: float, secs_left: float,
                 lookback: float = TWAP_LOOKBACK) -> float:
    """P(TWAP final ≥ target). Sin drift, log-normal."""
    if price <= 0 or target <= 0:
        return 0.5
    eff = twap_effective_seconds(secs_left, lookback)
    sd = sigma_sec * math.sqrt(eff)
    if sd <= 1e-12:
        return 1.0 if price >= target else 0.0
    z = math.log(price / target) / sd
    p = norm_cdf(z)
    return min(max(p, 0.005), 0.995)   # colas gordas: nunca 0% ni 100%


def taker_fee_per_share(p: float, fee: dict) -> float:
    """
    Comisión de taker por acción (USD), estimación CONSERVADORA:
        fee = rate · (p·(1−p))^exponent
    Con rate 0.07 y p = 0.50 → 1.75¢ por acción (3.5% de lo apostado).
    Verifica la fórmula exacta en docs.polymarket.com; si es menor, mejor para ti.
    """
    rate = float(fee.get("rate", DEFAULT_FEE["rate"]))
    exp = float(fee.get("exponent", DEFAULT_FEE["exponent"]))
    return rate * (p * (1 - p)) ** exp


def edge_after_fee(fair: float, ask: Optional[float], fee: dict) -> Optional[float]:
    if ask is None or ask <= 0 or ask >= 1:
        return None
    return fair - ask - taker_fee_per_share(ask, fee)


def ema(values: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def trend_5m(closes5: list[float]) -> int:
    """+1 alcista, −1 bajista, 0 lateral. EMA9 vs EMA21 + pendiente de EMA9."""
    if len(closes5) < 25:
        return 0
    e9, e21 = ema(closes5, 9), ema(closes5, 21)
    slope = (e9[-1] - e9[-4]) / e9[-4]
    gap = (e9[-1] - e21[-1]) / e21[-1]
    if gap > 0.0003 and slope > 0:
        return 1
    if gap < -0.0003 and slope < 0:
        return -1
    return 0


def book_imbalance(bids: list[tuple[float, float]], asks: list[tuple[float, float]],
                   band: float = 0.001) -> Optional[float]:
    """(bid_vol − ask_vol)/(total) dentro de ±0.1% del mid. +1 = presión compradora."""
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    bv = sum(q for p, q in bids if p >= mid * (1 - band))
    av = sum(q for p, q in asks if p <= mid * (1 + band))
    tot = bv + av
    return (bv - av) / tot if tot > 0 else None


# ───────────────────────────── DATOS ─────────────────────────────
def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_window_start(now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    return int(now // WINDOW * WINDOW)


def get_json(url: str, **kw):
    r = S.get(url, timeout=8, **kw)
    r.raise_for_status()
    return r.json()


def fetch_event(coin: str, start: int) -> Optional[dict]:
    slug = f"{coin}-updown-15m-{start}"
    data = get_json(f"{GAMMA}/events", params={"slug": slug})
    if not data:
        return None
    ev = data[0]
    m = ev["markets"][0]
    outcomes = json.loads(m.get("outcomes", '["Up","Down"]'))
    tokens = json.loads(m.get("clobTokenIds", "[]"))
    tok = dict(zip(outcomes, tokens))
    prices = json.loads(m.get("outcomePrices", "[]") or "[]")
    return {
        "slug": slug,
        "closed": bool(m.get("closed")),
        "up_token": tok.get("Up"),
        "down_token": tok.get("Down"),
        "best_bid": m.get("bestBid"),
        "best_ask": m.get("bestAsk"),
        "fee": m.get("feeSchedule") or DEFAULT_FEE,
        "lookback": (m.get("cryptoMarketConfig") or {}).get("twapLookbackSeconds", TWAP_LOOKBACK),
        "outcome_prices": dict(zip(outcomes, [float(x) for x in prices])) if prices else {},
    }


_OPEN_RE_CACHE: dict[str, re.Pattern] = {}


def parse_open_price_from_html(html: str, pm_symbol: str, start_iso: str) -> Optional[float]:
    """Saca el 'Price To Beat' (TWAP) que la página trae embebido en su estado inicial."""
    clean = html.replace("\\", "")
    key = f"{pm_symbol}|{start_iso}"
    if key not in _OPEN_RE_CACHE:
        _OPEN_RE_CACHE[key] = re.compile(
            r'"openPrice":([0-9.]+)[^\[\]]{0,1200}?"queryKey":\["crypto-prices","price","'
            + re.escape(pm_symbol) + r'","' + re.escape(start_iso) + r'","fifteen","[^"]+",true'
        )
    m = _OPEN_RE_CACHE[key].search(clean)
    return float(m.group(1)) if m else None


def fetch_target(coin: str, pm_symbol: str, start: int, source_fallback,
                 lookback: int = TWAP_LOOKBACK) -> tuple[Optional[float], str]:
    """Precio a superar. Orden: página de Polymarket (TWAP) → API crypto-price (TWAP) → open de Binance/HL."""
    start_iso = iso(start)
    try:
        r = S.get(f"{PM_WEB}/event/{coin}-updown-15m-{start}", timeout=10)
        if r.ok:
            v = parse_open_price_from_html(r.text, pm_symbol, start_iso)
            if v:
                return v, "PM"
    except requests.RequestException:
        pass
    try:
        j = get_json(f"{PM_WEB}/api/crypto/crypto-price", params={
            "symbol": pm_symbol, "eventStartTime": start_iso,
            "variant": "fifteen", "endDate": iso(start + WINDOW),
            # sin estos dos la API devuelve el open puntual, no el TWAP oficial
            "twapEnabled": "true", "twapLookbackSeconds": lookback})
        if j.get("openPrice"):
            return float(j["openPrice"]), "PM"
    except (requests.RequestException, ValueError):
        pass
    v = source_fallback(start)
    return v, "EXT"


def clob_best_ask(token_id: str) -> tuple[Optional[float], Optional[float]]:
    """(best_bid, best_ask) del libro de un token."""
    j = get_json(f"{CLOB}/book", params={"token_id": token_id})
    bids = [float(b["price"]) for b in j.get("bids", [])]
    asks = [float(a["price"]) for a in j.get("asks", [])]
    return (max(bids) if bids else None), (min(asks) if asks else None)


# ---- Binance ----
def bn_klines(sym: str, interval: str, limit: int, start_ms: Optional[int] = None) -> list[list]:
    p = {"symbol": sym, "interval": interval, "limit": limit}
    if start_ms:
        p["startTime"] = start_ms
    return get_json(f"{BINANCE}/api/v3/klines", params=p)


def bn_price(sym: str) -> float:
    return float(get_json(f"{BINANCE}/api/v3/ticker/price", params={"symbol": sym})["price"])


def bn_depth(sym: str):
    j = get_json(f"{BINANCE}/api/v3/depth", params={"symbol": sym, "limit": 100})
    return ([(float(p), float(q)) for p, q in j["bids"]],
            [(float(p), float(q)) for p, q in j["asks"]])


# ---- Hyperliquid ----
def hl_post(body: dict):
    r = S.post(HL, json=body, timeout=8)
    r.raise_for_status()
    return r.json()


def hl_klines(coin: str, interval: str, minutes: int) -> list[float]:
    end = int(time.time() * 1000)
    j = hl_post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval, "startTime": end - minutes * 60_000, "endTime": end}})
    return [float(c["c"]) for c in j]


def hl_price(coin: str) -> float:
    return float(hl_post({"type": "allMids"})[coin])


def hl_depth(coin: str):
    j = hl_post({"type": "l2Book", "coin": coin})
    bids, asks = j["levels"]
    return ([(float(x["px"]), float(x["sz"])) for x in bids],
            [(float(x["px"]), float(x["sz"])) for x in asks])


class Feed:
    """Precio, velas y libro para una moneda, sea Binance o Hyperliquid."""

    def __init__(self, source: str, sym: str):
        self.source, self.sym = source, sym
        self._k_cache: tuple[float, list[float], list[float]] = (0, [], [])

    def price(self) -> float:
        return bn_price(self.sym) if self.source == "binance" else hl_price(self.sym)

    def depth(self):
        return bn_depth(self.sym) if self.source == "binance" else hl_depth(self.sym)

    def candles(self) -> tuple[list[float], list[float]]:
        """(cierres 1m, cierres 5m), cacheado 30 s."""
        t, c1, c5 = self._k_cache
        if time.time() - t < 30 and c1:
            return c1, c5
        if self.source == "binance":
            c1 = [float(k[4]) for k in bn_klines(self.sym, "1m", VOL_LOOKBACK_MIN)]
            c5 = [float(k[4]) for k in bn_klines(self.sym, "5m", 60)]
        else:
            c1 = hl_klines(self.sym, "1m", VOL_LOOKBACK_MIN)
            c5 = hl_klines(self.sym, "5m", 300)
        self._k_cache = (time.time(), c1, c5)
        return c1, c5

    def open_at(self, start: int) -> Optional[float]:
        try:
            if self.source == "binance":
                k = bn_klines(self.sym, "1m", 1, start_ms=start * 1000)
                return float(k[0][1]) if k else None
            j = hl_post({"type": "candleSnapshot", "req": {
                "coin": self.sym, "interval": "1m",
                "startTime": start * 1000, "endTime": start * 1000 + 60_000}})
            return float(j[0]["o"]) if j else None
        except (requests.RequestException, IndexError, KeyError, ValueError):
            return None

    def twap_before(self, start: int, lookback: int = TWAP_LOOKBACK) -> Optional[float]:
        """Promedio del precio de ESTA fuente en los `lookback` s previos al inicio:
        los mismos segundos que promedia Chainlink para el "A superar"."""
        try:
            if self.source == "binance":
                k = bn_klines(self.sym, "1s", lookback, start_ms=(start - lookback) * 1000)
                closes = [float(x[4]) for x in k if int(x[0]) < start * 1000]
                if len(closes) >= lookback // 2:
                    return sum(closes) / len(closes)
                k = bn_klines(self.sym, "1m", 1, start_ms=(start - 60) * 1000)
                return (float(k[0][1]) + float(k[0][4])) / 2 if k else None
            j = hl_post({"type": "candleSnapshot", "req": {
                "coin": self.sym, "interval": "1m",
                "startTime": (start - 60) * 1000, "endTime": start * 1000 - 1}})
            return (float(j[0]["o"]) + float(j[0]["c"])) / 2 if j else None
        except (requests.RequestException, IndexError, KeyError, ValueError):
            return None


def measure_basis(feed_twap: Optional[float], target: Optional[float]) -> Optional[float]:
    """Base = fuente/oficial − 1 en el arranque de la ventana. None si no es confiable."""
    if not feed_twap or not target:
        return None
    b = feed_twap / target - 1
    return b if abs(b) <= MAX_BASIS else None


# ───────────────────────────── ESTADO / REGISTRO ─────────────────────────────
@dataclass
class Row:
    coin: str
    target: Optional[float] = None
    target_src: str = ""
    price: Optional[float] = None
    basis: float = 0.0           # Binance/Chainlink − 1 medido al abrir la ventana
    secs_left: float = 0
    sigma: Optional[float] = None
    fair_up: Optional[float] = None
    up_ask: Optional[float] = None
    down_ask: Optional[float] = None
    edge_up: Optional[float] = None
    edge_down: Optional[float] = None
    trend: int = 0
    imb: Optional[float] = None
    signal: str = ""
    note: str = ""


@dataclass
class Paper:
    trades: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Paper":
        if os.path.exists(PAPER_JSON):
            with open(PAPER_JSON) as f:
                return cls(json.load(f))
        return cls()

    def save(self):
        with open(PAPER_JSON, "w") as f:
            json.dump(self.trades, f, indent=1)

    def has(self, slug: str) -> bool:
        return any(t["slug"] == slug for t in self.trades)

    def add(self, slug: str, coin: str, side: str, ask: float, fair: float, fee: dict, secs_left: float,
            basis: float = 0.0):
        fee_ps = taker_fee_per_share(ask, fee)
        shares = PAPER_STAKE / (ask + fee_ps)
        self.trades.append({
            "v": MODEL_V, "basis_pb": round(basis * 1e4, 2),
            "slug": slug, "coin": coin, "side": side, "ask": ask, "fair": round(fair, 4),
            "fee_per_share": round(fee_ps, 5), "shares": round(shares, 4),
            "stake": PAPER_STAKE, "secs_left": int(secs_left),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": None, "pnl": None,
        })
        self.save()

    def settle(self):
        changed = False
        for t in self.trades:
            if t["result"] is not None:
                continue
            start = int(t["slug"].rsplit("-", 1)[1])
            if time.time() < start + WINDOW + 30:
                continue
            try:
                ev = fetch_event(t["coin"], start)
            except requests.RequestException:
                continue
            if not ev or not ev["outcome_prices"]:
                continue
            op = ev["outcome_prices"]
            if max(op.values()) < 0.99:      # todavía no resuelve oficialmente
                continue
            winner = max(op, key=op.get)
            t["result"] = "W" if winner == t["side"] else "L"
            t["pnl"] = round((t["shares"] if t["result"] == "W" else 0) - t["stake"], 4)
            changed = True
        if changed:
            self.save()

    def stats(self, v: Optional[int] = None) -> dict:
        trades = [t for t in self.trades if v is None or t.get("v", 1) == v]
        done = [t for t in trades if t["result"]]
        w = sum(1 for t in done if t["result"] == "W")
        pnl = sum(t["pnl"] for t in done)
        staked = sum(t["stake"] for t in done)
        exp_wr = (sum(t["fair"] for t in done) / len(done)) if done else 0
        return {"n": len(done), "open": len(trades) - len(done), "w": w,
                "wr": (w / len(done)) if done else 0, "pnl": pnl,
                "roi": (pnl / staked) if staked else 0, "exp_wr": exp_wr}


MARKET_COLS = ["hora", "ts", "slug", "moneda", "seg_rest", "precio", "base_pb", "target",
               "vol_1m", "prob_justa_up", "up_ask", "down_ask", "edge_up", "edge_down",
               "tendencia", "libro", "senal", "modelo"]


def _append_csv(path: str, cols: list[str], row: list) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(cols)
        w.writerow(row)


def log_snapshot(r: Row, slug: str) -> None:
    def rnd(x, n=4):
        return "" if x is None else round(x, n)
    _append_csv(MARKET_CSV, MARKET_COLS, [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(time.time()), slug, r.coin,
        int(r.secs_left), r.price, round(r.basis * 1e4, 2), r.target,
        rnd(r.sigma * math.sqrt(60) if r.sigma else None, 6), rnd(r.fair_up), r.up_ask, r.down_ask,
        rnd(r.edge_up), rnd(r.edge_down), r.trend, rnd(r.imb, 3), r.signal, MODEL_V])


class Recorder:
    """Foto de cada moneda una vez por minuto + ganador de cada ventana terminada."""

    def __init__(self):
        self.last_shot: dict[str, float] = {}
        self.pending: set[tuple[str, int]] = set()     # (coin, start) por resolver
        self.done: set[str] = set()
        if os.path.exists(RESULTS_CSV):
            with open(RESULTS_CSV) as f:
                self.done = {row["slug"] for row in csv.DictReader(f)}

    def snapshot(self, coin: str, start: int, r: Row) -> None:
        self.pending.add((coin, start))
        if r.fair_up is None or time.time() - self.last_shot.get(coin, 0) < SNAPSHOT_EVERY:
            return
        self.last_shot[coin] = time.time()
        log_snapshot(r, f"{coin}-updown-15m-{start}")

    def resolve(self) -> None:
        now = time.time()
        for coin, start in list(self.pending):
            slug = f"{coin}-updown-15m-{start}"
            if slug in self.done:
                self.pending.discard((coin, start))
                continue
            if now < start + WINDOW + 60:
                continue
            if now > start + WINDOW + 6 * 3600:          # no resolvió en 6 h: se abandona
                self.pending.discard((coin, start))
                continue
            try:
                ev = fetch_event(coin, start)
            except requests.RequestException:
                continue
            op = (ev or {}).get("outcome_prices") or {}
            if not op or max(op.values()) < 0.99:
                continue
            _append_csv(RESULTS_CSV, ["slug", "moneda", "inicio", "ganador"],
                        [slug, coin.upper(), start, max(op, key=op.get)])
            self.done.add(slug)
            self.pending.discard((coin, start))


def log_signal(r: Row, slug: str, side: str):
    new = not os.path.exists(SIGNALS_CSV)
    with open(SIGNALS_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["hora", "slug", "moneda", "lado", "precio", "target", "seg_rest",
                        "prob_justa_up", "up_ask", "down_ask", "edge", "tendencia", "libro"])
        edge = r.edge_up if side == "Up" else r.edge_down
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), slug, r.coin, side, r.price,
                    r.target, int(r.secs_left), round(r.fair_up or 0, 4), r.up_ask, r.down_ask,
                    round(edge or 0, 4), r.trend, round(r.imb or 0, 3)])


# ───────────────────────────── LÓGICA DE SEÑAL ─────────────────────────────
def decide(r: Row) -> tuple[str, str]:
    """Devuelve (lado o '', razón)."""
    if r.fair_up is None:
        return "", "sin datos"
    if r.secs_left < MIN_SECONDS_LEFT:
        return "", "últimos 2 min"
    best = None
    for side, ask, edge in (("Up", r.up_ask, r.edge_up), ("Down", r.down_ask, r.edge_down)):
        if ask is None or edge is None:
            continue
        if not (PRICE_MIN <= ask <= PRICE_MAX):
            continue
        if edge < MIN_EDGE:
            continue
        if TREND_FILTER and ((side == "Up" and r.trend < 0) or (side == "Down" and r.trend > 0)):
            continue
        if best is None or edge > best[1]:
            best = (side, edge)
    if best:
        return best[0], f"+{best[1]*100:.1f}¢"
    return "", "sin ventaja"


BASIS_LAST: dict[str, float] = {}   # última base medida por moneda (respaldo si falla la medición)


def scan_coin(coin: str, feed: Feed, pm_symbol: str, start: int, cache: dict) -> tuple[Row, Optional[dict]]:
    r = Row(coin=coin.upper())
    now = time.time()
    r.secs_left = start + WINDOW - now

    key = (coin, start)
    if key not in cache:
        ev = fetch_event(coin, start)
        if not ev:
            r.note = "sin mercado"
            return r, None
        target, src = fetch_target(coin, pm_symbol, start, feed.open_at, ev["lookback"])
        cache[key] = {"ev": ev, "target": target, "src": src, "t": now, "basis": None}
    c = cache[key]
    # reintenta el precio a superar oficial si salió de fuente externa
    if c["src"] != "PM" and now - c["t"] > 20:
        t2, s2 = fetch_target(coin, pm_symbol, start, feed.open_at, c["ev"]["lookback"])
        if t2:
            c.update(target=t2, src=s2)
        c["t"] = now
    ev = c["ev"]
    r.target, r.target_src = c["target"], c["src"]

    # Base Binance↔Chainlink de esta ventana (una vez). Solo tiene sentido contra el
    # "A superar" oficial; si salió de la misma fuente (EXT), la base es 0.
    if MODEL_V >= 2 and c["src"] == "PM" and c["basis"] is None:
        b = measure_basis(feed.twap_before(start, ev["lookback"]), c["target"])
        if b is not None:
            c["basis"] = b
            BASIS_LAST[coin] = b
    r.basis = (c["basis"] if c["basis"] is not None else BASIS_LAST.get(coin, 0.0)) \
        if (MODEL_V >= 2 and c["src"] == "PM") else 0.0

    r.price = feed.price()
    c1, c5 = feed.candles()
    r.sigma = ewma_sigma_per_sec(c1 + [r.price])
    r.trend = trend_5m(c5)
    try:
        r.imb = book_imbalance(*feed.depth())
    except (requests.RequestException, KeyError, IndexError, ValueError):
        r.imb = None

    try:
        _, r.up_ask = clob_best_ask(ev["up_token"])
        _, r.down_ask = clob_best_ask(ev["down_token"])
    except (requests.RequestException, KeyError, ValueError, TypeError):
        r.up_ask = float(ev["best_ask"]) if ev.get("best_ask") else None
        r.down_ask = round(1 - float(ev["best_bid"]), 3) if ev.get("best_bid") else None

    if r.target and r.sigma:
        # precio de Binance llevado a la escala de Chainlink
        r.fair_up = fair_prob_up(r.price / (1 + r.basis), r.target, r.sigma, r.secs_left, ev["lookback"])
        r.edge_up = edge_after_fee(r.fair_up, r.up_ask, ev["fee"])
        r.edge_down = edge_after_fee(1 - r.fair_up, r.down_ask, ev["fee"])
    r.signal, r.note = decide(r)
    return r, ev


# ───────────────────────────── PANTALLA ─────────────────────────────
def fmt_price(x: Optional[float]) -> str:
    if x is None:
        return "—"
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.4f}"
    return f"{x:.5f}"


def render(rows: list[Row], paper: Paper, start: int):
    from rich.table import Table
    from rich.panel import Panel
    from rich.console import Group
    from rich.text import Text

    end_local = datetime.fromtimestamp(start + WINDOW).strftime("%H:%M")
    start_local = datetime.fromtimestamp(start).strftime("%H:%M")
    left = max(0, start + WINDOW - time.time())
    title = (f"[bold]SCANNER 15m · Polymarket Up/Down[/]   ventana {start_local}–{end_local}   "
             f"[bold yellow]⏱ {int(left)//60:02d}:{int(left)%60:02d}[/]")

    t = Table(expand=True, header_style="bold cyan", show_lines=False)
    for col, j in [("Moneda", "left"), ("A superar", "right"), ("Actual", "right"), ("Δ%", "right"),
                   ("Base pb", "right"), ("Vol 1m", "right"), ("Justa Up", "right"), ("Up ¢", "right"), ("Down ¢", "right"),
                   ("Edge Up", "right"), ("Edge Down", "right"), ("Tend 5m", "center"),
                   ("Libro", "right"), ("SEÑAL", "center")]:
        t.add_column(col, justify=j)

    for r in rows:
        if r.target is None or r.price is None:
            t.add_row(r.coin, "—", fmt_price(r.price), "", "", "", "", "", "", "", "", "", "", r.note or "…")
            continue
        d = (r.price / (1 + r.basis) / r.target - 1) * 100
        dcol = "green" if d >= 0 else "red"
        tgt = fmt_price(r.target) + ("" if r.target_src == "PM" else f" [dim]{r.target_src}[/]")
        vol = f"{r.sigma*math.sqrt(60)*100:.3f}%" if r.sigma else "—"

        def e(x):
            if x is None:
                return "—"
            c = "green" if x >= MIN_EDGE else ("yellow" if x > 0 else "red")
            return f"[{c}]{x*100:+.1f}[/]"

        tr = {1: "[green]▲[/]", -1: "[red]▼[/]", 0: "[dim]—[/]"}[r.trend]
        imb = "—" if r.imb is None else (f"[green]{r.imb:+.2f}[/]" if r.imb > 0.15 else
                                         f"[red]{r.imb:+.2f}[/]" if r.imb < -0.15 else f"{r.imb:+.2f}")
        if r.signal == "Up":
            sig = f"[bold black on green] UP {r.note} [/]"
        elif r.signal == "Down":
            sig = f"[bold white on red] DOWN {r.note} [/]"
        else:
            sig = f"[dim]{r.note}[/]"
        t.add_row(r.coin, tgt, fmt_price(r.price), f"[{dcol}]{d:+.3f}[/]", f"{r.basis*1e4:+.1f}", vol,
                  f"{(r.fair_up or 0)*100:.1f}%",
                  "—" if r.up_ask is None else f"{r.up_ask*100:.0f}",
                  "—" if r.down_ask is None else f"{r.down_ask*100:.0f}",
                  e(r.edge_up), e(r.edge_down), tr, imb, sig)

    st = paper.stats(MODEL_V)
    old = paper.stats(1)
    pnl_col = "green" if st["pnl"] >= 0 else "red"
    foot = Text.from_markup(
        f"[bold]Papel modelo v{MODEL_V} (${PAPER_STAKE:.0f} por señal):[/] "
        f"{st['n']} cerradas · {st['open']} abiertas · aciertos {st['w']}/{st['n']} "
        f"({st['wr']*100:.0f}% vs {st['exp_wr']*100:.0f}% esperado) · "
        f"P&L [{pnl_col}]{st['pnl']:+.2f} USD[/] · ROI [{pnl_col}]{st['roi']*100:+.1f}%[/]\n"
        f"[dim]Modelo v1 (anterior, sin corrección): {old['n']} cerradas · "
        f"{old['wr']*100:.0f}% aciertos · P&L {old['pnl']:+.2f} USD[/]\n"
        f"[dim]Edge = prob. justa − precio − comisión taker. Señal: edge ≥ {MIN_EDGE*100:.0f}¢, "
        f"precio {PRICE_MIN*100:.0f}–{PRICE_MAX*100:.0f}¢, >2 min restantes, sin ir contra la tendencia. "
        f"Herramienta de análisis, no consejo financiero. Toda apuesta puede perderse.[/]"
    )
    return Panel(Group(t, foot), title=title, border_style="bright_blue")


# ───────────────────────────── MAIN ─────────────────────────────
def run_cycle(coins: list[str], feeds: dict, paper: Paper, cache: dict, on_signal=None,
              recorder: Optional["Recorder"] = None) -> tuple[int, list[Row]]:
    """Un ciclo: escanea cada moneda, registra señales nuevas y liquida el papel."""
    start = current_window_start()
    for k in [k for k in cache if k[1] != start]:   # limpia mercados viejos del cache
        cache.pop(k)
    rows = []
    for coin in coins:
        try:
            r, ev = scan_coin(coin, feeds[coin], COINS[coin][2], start, cache)
        except (requests.RequestException, ValueError, KeyError) as e:
            r, ev = Row(coin=coin.upper(), note=f"error: {type(e).__name__}"), None
        rows.append(r)
        if recorder and ev:
            recorder.snapshot(coin, start, r)
        if r.signal and ev and not paper.has(ev["slug"]):
            ask = r.up_ask if r.signal == "Up" else r.down_ask
            fair = r.fair_up if r.signal == "Up" else 1 - r.fair_up
            paper.add(ev["slug"], coin, r.signal, ask, fair, ev["fee"], r.secs_left, r.basis)
            log_signal(r, ev["slug"], r.signal)
            if on_signal:
                on_signal(r, ask)
    try:
        paper.settle()
        if recorder:
            recorder.resolve()
    except Exception:
        pass
    return start, rows


def log_line(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def summary(paper: Paper) -> str:
    st, old = paper.stats(MODEL_V), paper.stats(1)
    return (f"papel v{MODEL_V}: {st['n']} cerradas, {st['open']} abiertas, "
            f"{st['w']}/{st['n']} aciertos ({st['wr']*100:.0f}% vs {st['exp_wr']*100:.0f}% esperado), "
            f"P&L {st['pnl']:+.2f} USD | v1: {old['n']} cerradas, P&L {old['pnl']:+.2f}")


def main_headless(coins: list[str]) -> None:
    """Sin pantalla (servidor): una línea por señal y un resumen cada 5 min."""
    feeds = {c: Feed(COINS[c][0], COINS[c][1]) for c in coins}
    paper, cache, rec = Paper.load(), {}, Recorder()
    log_line(f"Scanner 15m (sin pantalla) · modelo v{MODEL_V} · monedas {', '.join(coins)} · datos en {DATA_DIR}")
    last = 0.0

    def on_signal(r: Row, ask: float) -> None:
        log_line(f"SEÑAL {r.coin} {r.signal.upper()} @ {ask*100:.0f}¢ {r.note} · base {r.basis*1e4:+.1f} pb · "
                 f"quedan {int(r.secs_left)} s")

    while True:
        try:
            _, rows = run_cycle(coins, feeds, paper, cache, on_signal, rec)
            if time.time() - last > 300:
                errs = [f"{r.coin}: {r.note}" for r in rows if r.note.startswith("error")]
                log_line(summary(paper) + (f" | errores: {', '.join(errs)}" if errs else ""))
                last = time.time()
        except Exception as e:                     # nunca morir por un error suelto
            log_line(f"error en el ciclo: {e}")
        time.sleep(REFRESH)


def main():
    global TREND_FILTER, MIN_EDGE
    ap = argparse.ArgumentParser(description="Scanner Polymarket 15m Up/Down")
    ap.add_argument("--coins", nargs="+", default=list(COINS), choices=list(COINS))
    ap.add_argument("--no-trend", action="store_true", help="desactiva el filtro de tendencia")
    ap.add_argument("--min-edge", type=float, default=MIN_EDGE)
    ap.add_argument("--headless", action="store_true", help="sin pantalla, para servidor (log de texto)")
    args = ap.parse_args()

    TREND_FILTER = not args.no_trend
    MIN_EDGE = args.min_edge

    if args.headless:
        main_headless(args.coins)
        return

    from rich.live import Live
    from rich.console import Console

    feeds = {c: Feed(COINS[c][0], COINS[c][1]) for c in args.coins}
    paper = Paper.load()
    cache: dict = {}
    rec = Recorder()
    console = Console()

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while True:
            start, rows = run_cycle(args.coins, feeds, paper, cache,
                                    on_signal=lambda r, ask: live.console.bell(),   # campanita
                                    recorder=rec)
            live.update(render(rows, paper, start))
            time.sleep(REFRESH)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
