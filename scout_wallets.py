#!/usr/bin/env python3
"""
Scout de wallets para Smart Money copytrading.

Dos modos (elige automáticamente):
  1. MANUAL — si existe wallets_candidates.txt en esta carpeta (una dirección
     0x... por línea, '#' comenta), analiza y rankea ESAS wallets. Es el modo
     confiable: entra a polymarket.com/leaderboard, copia las addresses de los
     traders top y pégalas ahí.
  2. AUTO — si no hay archivo, intenta bajar el leaderboard de Polymarket
     (el endpoint cambia seguido; si todos fallan, te dice cómo usar el modo 1).

En ambos casos rankea por compatibilidad con los filtros del bot (entradas en
zona 0.15-0.85, actividad razonable, sin mercados crypto-price) y al final
imprime un bloque JSON listo para pegar en wallets.json.

Uso (correr donde haya acceso a la API pública de Polymarket, ej. tu Mac):
    python3 scout_wallets.py

Solo lectura: no necesita credenciales ni private key.
"""

import json
import os
import time
from datetime import datetime, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
HEADERS = {
    "Accept": "application/json",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

CANDIDATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "wallets_candidates.txt")

# Mismos keywords que usa el bot para excluir mercados crypto-price
CRYPTO_PRICE_KEYWORDS = frozenset([
    "bitcoin", "btc", "ethereum", "eth", "crypto", "price", "solana", "sol",
    "xrp", "ripple", "doge", "dogecoin", "bnb", "usdc", "usdt", "stablecoin",
    "altcoin", "defi", "nft", "token", "coin",
])

# El leaderboard de Polymarket ha cambiado de host/ruta varias veces.
# Probamos varios candidatos; si tu diagnóstico halla uno nuevo que da 200,
# agrégalo a esta lista.
LEADERBOARD_CANDIDATES = [
    ("https://lb-api.polymarket.com/leaderboard", {"window": "1m", "rankType": "pnl", "limit": 50}),
    ("https://lb-api.polymarket.com/rank",        {"window": "1m", "limit": 50}),
    ("https://lb-api.polymarket.com/pnl",         {"window": "1m", "limit": 50}),
    ("https://polymarket.com/api/leaderboard",    {"window": "1m", "limit": 50}),
    (f"{DATA_API}/leaderboard",                   {"window": "1m", "rankType": "pnl", "limit": 50}),
    (f"{DATA_API}/rankings",                      {"window": "1m", "limit": 50}),
]


def load_candidates() -> list[dict]:
    """Lee wallets_candidates.txt: una dirección 0x... por línea, '#' comenta.
    Formato opcional: '0xADDRESS  nombre' (nombre tras espacios)."""
    if not os.path.exists(CANDIDATES_FILE):
        return []
    entries = []
    with open(CANDIDATES_FILE) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            addr = parts[0]
            # Dirección Ethereum válida: 0x + 40 hex
            if not (addr.startswith("0x") and len(addr) == 42):
                print(f"  ⚠ ignorada (no parece address válida): {addr}")
                continue
            try:
                int(addr[2:], 16)
            except ValueError:
                print(f"  ⚠ ignorada (no es hex): {addr}")
                continue
            name = " ".join(parts[1:]) if len(parts) > 1 else ""
            entries.append({"proxyWallet": addr, "name": name})
    return entries


def fetch_leaderboard() -> list[dict]:
    for url, params in LEADERBOARD_CANDIDATES:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            print(f"  {r.status_code} ← {url}")
            if r.status_code != 200:
                continue
            data = r.json()
            if isinstance(data, dict):
                data = data.get("leaderboard") or data.get("data") or []
            if isinstance(data, list) and data:
                return data
        except Exception as e:
            print(f"  error ← {url}: {e}")
    return []


def get_address(entry: dict) -> str:
    for key in ("proxyWallet", "address", "wallet", "user"):
        if entry.get(key):
            return str(entry[key])
    return ""


def get_name(entry: dict) -> str:
    for key in ("name", "userName", "username", "pseudonym"):
        if entry.get(key):
            return str(entry[key])
    return ""


def get_pnl(entry: dict) -> float:
    for key in ("amount", "pnl", "profit", "cashPnl"):
        try:
            if entry.get(key) is not None:
                return float(entry[key])
        except (ValueError, TypeError):
            continue
    return 0.0


def analyze_wallet(address: str) -> dict | None:
    """Métricas de compatibilidad con los filtros del bot."""
    try:
        r = requests.get(f"{DATA_API}/trades",
                         params={"user": address, "limit": 100},
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        trades = r.json()
        if not isinstance(trades, list) or not trades:
            return None
    except Exception:
        return None

    now = datetime.now(timezone.utc).timestamp()
    buys = [t for t in trades if str(t.get("side", "")).upper() == "BUY"]
    if not buys:
        return None

    def is_crypto(t):
        q = str(t.get("title", "")).lower()
        return any(kw in q for kw in CRYPTO_PRICE_KEYWORDS)

    in_zone = 0
    non_crypto = 0
    sizes = []
    oldest_ts = now
    for t in buys:
        try:
            price = float(t.get("price", 0))
            ts = float(t.get("timestamp", now))
            size = float(t.get("size", 0)) * price
        except (ValueError, TypeError):
            continue
        oldest_ts = min(oldest_ts, ts)
        sizes.append(size)
        if 0.15 <= price <= 0.85:
            in_zone += 1
        if not is_crypto(t):
            non_crypto += 1

    weeks = max((now - oldest_ts) / (7 * 24 * 3600), 0.15)
    return {
        "trades_per_week": round(len(buys) / weeks, 1),
        "pct_in_zone": round(100 * in_zone / len(buys)),
        "pct_non_crypto": round(100 * non_crypto / len(buys)),
        "avg_size_usd": round(sum(sizes) / len(sizes), 0) if sizes else 0,
        "n_buys_sampled": len(buys),
    }


def score(metrics: dict, pnl: float) -> float:
    """Score de compatibilidad: PnL positivo, entradas copiables y actividad
    razonable (ni muerta ni HFT imposible de seguir)."""
    tpw = metrics["trades_per_week"]
    if tpw < 1:
        activity = 0.3          # muy poca actividad: casi nunca habrá señal
    elif tpw <= 40:
        activity = 1.0          # sweet spot
    else:
        activity = 0.5          # demasiados trades: imposible copiar bien
    zone = metrics["pct_in_zone"] / 100
    clean = metrics["pct_non_crypto"] / 100
    pnl_norm = min(max(pnl, 0) / 10_000, 1.0)
    return round((0.4 * pnl_norm + 0.3 * zone + 0.2 * clean + 0.1 * activity) * 100, 1)


def main():
    # Modo 1 (manual): si hay lista pegada, la usamos — es lo más confiable.
    board = load_candidates()
    if board:
        print(f"📋 Modo manual: {len(board)} wallets desde wallets_candidates.txt\n")
    else:
        # Modo 2 (auto): intentar el leaderboard.
        print("Buscando leaderboard (no hay wallets_candidates.txt)...")
        board = fetch_leaderboard()
        if not board:
            print("\n❌ No se pudo bajar el leaderboard con ningún endpoint.")
            print("   USA EL MODO MANUAL (más confiable):")
            print("   1. Entra a polymarket.com/leaderboard")
            print("   2. Copia las direcciones 0x... de los traders top")
            print(f"   3. Pégalas (una por línea) en:\n      {CANDIDATES_FILE}")
            print("   4. Vuelve a correr: python3 scout_wallets.py")
            return
        print(f"Leaderboard: {len(board)} traders. Analizando actividad (~1 min)...\n")

    results = []
    for entry in board[:30]:
        address = get_address(entry)
        if not address:
            continue
        name = get_name(entry) or address[:10]
        pnl = get_pnl(entry)
        metrics = analyze_wallet(address)
        time.sleep(0.4)  # no golpear la API
        if not metrics:
            print(f"  {name:<22} sin datos de trades, saltando")
            continue
        s = score(metrics, pnl)
        results.append({"address": address, "name": name, "pnl": pnl,
                        "score": s, **metrics})
        print(f"  {name:<22} score={s:>5} | pnl=${pnl:>10,.0f} | "
              f"{metrics['trades_per_week']:>5}/sem | zona {metrics['pct_in_zone']}% | "
              f"no-crypto {metrics['pct_non_crypto']}%")

    if not results:
        print("\n❌ Ninguna wallet con datos suficientes.")
        return

    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[:5]

    print("\n" + "═" * 70)
    print("TOP 5 CANDIDATAS — pega las que quieras en wallets.json:")
    print("═" * 70)
    wallets_block = [
        {"address": r["address"], "name": r["name"],
         "categories": f"scout score={r['score']} pnl=${r['pnl']:,.0f}"}
        for r in top
    ]
    print(json.dumps(wallets_block, indent=2, ensure_ascii=False))
    print("\nConsejo: mezcla categorías (deportes, geopolítica, economía) y")
    print("mantén las wallets actuales que ya te dieron PnL positivo.")


if __name__ == "__main__":
    main()
