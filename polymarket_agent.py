"""
╔══════════════════════════════════════════════════════════════╗
║     POLYMARKET AI AGENT v2.0 — powered by Claude            ║
║     Abre · Monitorea · Cierra · Reinvierte ganancias         ║
╚══════════════════════════════════════════════════════════════╝
 
CICLO COMPLETO:
  1. Scanner    → Descarga mercados activos de Polymarket
  2. Filter     → Filtra por liquidez, volumen y tiempo
  3. Analyzer   → Claude + web search estima probabilidad real
  4. Edge Calc  → Detecta discrepancia precio mercado vs IA
  5. Opener     → Abre posición con Kelly Criterion
  6. Monitor    → Revisa posiciones abiertas cada ciclo
  7. Closer     → Cierra si: take profit / stop loss / mercado resuelto
  8. Compounder → Reinvierte ganancias al bankroll dinámico
 
REQUISITOS:
  pip install anthropic py-clob-client python-dotenv requests
"""
 
import os
import re
import sys
import json
import time
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv
 
import anthropic
import requests
 
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import OrderArgs, ApiCreds
    from py_clob_client_v2.constants import POLYGON
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    logging.warning("py-clob-client-v2 no instalado. Corriendo en modo DRY RUN.")
 
load_dotenv()
 
# ═══════════════════════════════════════════════════════════
#  CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════
CONFIG = {
    "ANTHROPIC_API_KEY":    os.getenv("ANTHROPIC_API_KEY", ""),
    "PRIVATE_KEY":          os.getenv("POLYMARKET_PRIVATE_KEY", ""),
    "PROXY_ADDRESS":        os.getenv("POLYMARKET_PROXY_ADDRESS") or os.getenv("PROXY_ADDRESS", ""),
    "API_KEY":              os.getenv("POLYMARKET_API_KEY", ""),
    "API_SECRET":           os.getenv("POLYMARKET_API_SECRET", ""),
    "API_PASSPHRASE":       os.getenv("POLYMARKET_API_PASSPHRASE", ""),
 
    # ── Bankroll ────────────────────────────────────────────
    "BANKROLL":             float(os.getenv("BANKROLL", "100")),
    "MIN_BET_USD":          float(os.getenv("MIN_BET_USD", "2.0")),
    "MAX_BET_USD":          float(os.getenv("MAX_BET_USD", "15.0")),
    "MAX_BET_PCT":          float(os.getenv("MAX_BET_PCT", "0.05")),   # 5% del bankroll actual
 
    # ── Edge y señales ──────────────────────────────────────
    "MIN_EDGE":             float(os.getenv("MIN_EDGE", "0.06")),       # 6% mínimo para abrir
 
    # ── Take Profit / Stop Loss ─────────────────────────────
    # Cerrar posición si el precio sube X% desde entrada (ganancia)
    "TAKE_PROFIT_PCT":      float(os.getenv("TAKE_PROFIT_PCT", "0.30")),  # +30% sobre entrada
    # Cerrar si el precio baja X% desde entrada (pérdida)
    "STOP_LOSS_PCT":        float(os.getenv("STOP_LOSS_PCT", "0.40")),    # -40% sobre entrada
    # Cerrar si quedan menos de N días para resolución (capturar liquidez)
    "CLOSE_DAYS_LEFT":      int(os.getenv("CLOSE_DAYS_LEFT", "3")),
    # Cerrar si el edge ya desapareció (mercado corrigió)
    "CLOSE_IF_EDGE_GONE":   os.getenv("CLOSE_IF_EDGE_GONE", "false").lower() == "true",

    # ── Riesgo global ───────────────────────────────────────
    "MAX_OPEN_BETS":        int(os.getenv("MAX_OPEN_BETS", "8")),
    "MAX_DAILY_LOSS":       float(os.getenv("MAX_DAILY_LOSS", "20")),
    "MIN_LIQUIDITY":        float(os.getenv("MIN_LIQUIDITY", "5000")),
    "MIN_VOLUME":           float(os.getenv("MIN_VOLUME", "5000")),

    # ── Operación ───────────────────────────────────────────
    "SCAN_INTERVAL_MIN":    int(os.getenv("SCAN_INTERVAL_MIN", "60")),
    "MAX_MARKETS_PER_RUN":  int(os.getenv("MAX_MARKETS_PER_RUN", "5")),
    "PRE_FILTER_BATCH_SIZE": int(os.getenv("PRE_FILTER_BATCH_SIZE", "40")),  # max mercados al pre-filtro
    "PRE_FILTER_TOP_N":      int(os.getenv("PRE_FILTER_TOP_N", "5")),        # cuántos pasan a análisis profundo
    "DRY_RUN":              os.getenv("DRY_RUN", "true").lower() == "true",
    "STATE_FILE":           "agent_state.json",
    "LOG_FILE":             "polymarket_agent.log",

    # ── Smart Money Following ──────────────────────────────────────────
    "SMART_MONEY_ENABLED":      os.getenv("SMART_MONEY_ENABLED", "true").lower() == "true",
    "SMART_MONEY_MAX_HOURS":    int(os.getenv("SMART_MONEY_MAX_HOURS", "72")),
    "SMART_MONEY_MAX_COPIES":   int(os.getenv("SMART_MONEY_MAX_COPIES", "2")),
    "SMART_MONEY_BET_PCT":      float(os.getenv("SMART_MONEY_BET_PCT", "0.03")),
    "SMART_MONEY_MAX_SLIPPAGE": float(os.getenv("SMART_MONEY_MAX_SLIPPAGE", "0.10")),

    # ── Contrarian Fade ─────────────────────────────────
    "CONTRARIAN_ENABLED":      os.getenv("CONTRARIAN_ENABLED", "true").lower() == "true",
    "CONTRARIAN_MIN_MOVE":     float(os.getenv("CONTRARIAN_MIN_MOVE", "0.10")),
    "CONTRARIAN_MAX_MOVE":     float(os.getenv("CONTRARIAN_MAX_MOVE", "0.40")),
    "CONTRARIAN_BET_USD":      float(os.getenv("CONTRARIAN_BET_USD", "2.0")),
    "CONTRARIAN_MAX_POSITIONS": int(os.getenv("CONTRARIAN_MAX_POSITIONS", "2")),
    "CONTRARIAN_LOOKBACK_HOURS": int(os.getenv("CONTRARIAN_LOOKBACK_HOURS", "24")),
    "CONTRARIAN_PRICE_MIN":    float(os.getenv("CONTRARIAN_PRICE_MIN", "0.30")),
    "CONTRARIAN_PRICE_MAX":    float(os.getenv("CONTRARIAN_PRICE_MAX", "0.70")),
    "CONTRARIAN_MIN_DAYS_LEFT": int(os.getenv("CONTRARIAN_MIN_DAYS_LEFT", "2")),
    "CONTRARIAN_MAX_MARKETS_TO_SCAN": int(os.getenv("CONTRARIAN_MAX_MARKETS_TO_SCAN", "30")),
}
 
# ═══════════════════════════════════════════════════════════
#  SMART MONEY WALLETS
# ═══════════════════════════════════════════════════════════
SMART_WALLETS = [
    {
        "address":    "0x30d1c420d1abde9442d6762dd6f6d5f92df04525",
        "name":       "randomWalkingS",
        "categories": "Geopolítica/China/Trump",
    },
    {
        "address":    "0xffb0b9b292e406fd250854a35a0c9bd5612aFa37",
        "name":       "BloodyMummer",
        "categories": "Geopolítica/Iran/Israel",
    },
    {
        "address":    "0x07921379f7b31ef93da634b688b2fe36897db778",
        "name":       "ewelmealt",
        "categories": "Fútbol/La Liga",
    },
    {
        "address":    "0x92672c80d36dcd08172aa1e51dface0f20b70f9a",
        "name":       "CKW",
        "categories": "UFC/MLB",
    },
]

POLYMARKET_DATA_API = "https://data-api.polymarket.com"

# ═══════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"]),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)
 
 
# ═══════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════
@dataclass
class Market:
    condition_id: str
    question: str
    category: str
    volume: float
    liquidity: float
    end_date: str
    outcomes: list[dict]
    url: str = ""
 
@dataclass
class Opportunity:
    market: Market
    outcome_name: str
    token_id: str
    market_price: float
    ai_probability: float
    edge: float
    kelly_fraction: float
    bet_size_usd: float
    reasoning: str
    confidence: str
 
@dataclass
class Position:
    id: str                    # UUID único
    market_condition_id: str
    market_question: str
    outcome: str
    token_id: str
    size_usd: float            # dinero apostado
    shares: float              # cantidad de shares comprados
    entry_price: float         # precio al entrar (0-1)
    ai_probability: float      # probabilidad que estimó la IA al entrar
    end_date: str              # fecha de resolución
    opened_at: str
    status: str = "OPEN"       # OPEN | CLOSED_PROFIT | CLOSED_LOSS | RESOLVED
 
@dataclass
class CloseDecision:
    should_close: bool
    reason: str
    current_price: float = 0.0
    unrealized_pnl_usd: float = 0.0
    unrealized_pnl_pct: float = 0.0
 
@dataclass
class AgentState:
    bankroll: float            # bankroll dinámico — crece con ganancias
    initial_bankroll: float
    open_positions: list[Position] = field(default_factory=list)
    closed_positions: list[Position] = field(default_factory=list)
    total_invested: float = 0.0
    total_returned: float = 0.0
    daily_pnl: float = 0.0
    daily_loss_triggered: bool = False
    analyzed_today: set = field(default_factory=set)
    session_start: str = field(default_factory=lambda: datetime.now().isoformat())
 
    @property
    def total_pnl(self) -> float:
        return self.bankroll - self.initial_bankroll
 
    @property
    def total_pnl_pct(self) -> float:
        if self.initial_bankroll == 0:
            return 0
        return (self.total_pnl / self.initial_bankroll) * 100
 
    @property
    def win_rate(self) -> float:
        closed = [p for p in self.closed_positions if p.status != "OPEN"]
        if not closed:
            return 0
        wins = sum(1 for p in closed if p.status == "CLOSED_PROFIT")
        return (wins / len(closed)) * 100
 
 
# ═══════════════════════════════════════════════════════════
#  POLYMARKET API
# ═══════════════════════════════════════════════════════════
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
CLOB_PRICES_HISTORY = "https://clob.polymarket.com/prices-history"
 
class PolymarketScanner:
 
    def get_active_markets(self, limit=300) -> list[Market]:
        # Intentar con diferentes combinaciones de parámetros
        param_sets = [
            {"active": "true", "limit": limit, "order": "volume24hr", "ascending": "false"},
            {"active": "true", "closed": "false", "limit": limit, "order": "volume24hr", "ascending": "false"},
            {"limit": limit, "order": "volume24hr", "ascending": "false"},
            {"active": "true", "limit": limit},
        ]
 
        for params in param_sets:
            try:
                resp = requests.get(
                    f"{GAMMA_API}/markets",
                    params=params,
                    timeout=20,
                    headers={"Accept": "application/json"}
                )
                resp.raise_for_status()
                data = resp.json()
 
                # La API puede devolver lista o dict con paginación
                if isinstance(data, dict):
                    raw = data.get("data", data.get("markets", []))
                elif isinstance(data, list):
                    raw = data
                else:
                    log.warning(f"Formato inesperado de API: {type(data)}")
                    continue
 
                if not raw:
                    log.warning(f"API devolvió 0 mercados con params: {params}")
                    continue
 
                markets = []
                for m in raw:
                    try:
                        outcomes = self._parse_outcomes(m)
                        if not outcomes:
                            continue
                        markets.append(Market(
                            condition_id=m.get("conditionId", m.get("id", "")),
                            question=m.get("question", ""),
                            category=m.get("category", "Other"),
                            volume=float(m.get("volume", 0) or 0),
                            liquidity=float(m.get("liquidity", 0) or 0),
                            end_date=m.get("endDate", ""),
                            outcomes=outcomes,
                            url=f"https://polymarket.com/event/{m.get('slug', '')}",
                        ))
                    except Exception:
                        continue
 
                if markets:
                    log.info(f"Descargados {len(markets)} mercados")
                    return markets
 
            except Exception as e:
                log.error(f"Error descargando mercados con params {params}: {e}")
                continue
 
        # Último intento: usar el endpoint de eventos
        try:
            log.info("Intentando endpoint de eventos como fallback...")
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={"active": "true", "limit": 50, "order": "volume24hr", "ascending": "false"},
                timeout=20,
                headers={"Accept": "application/json"}
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data if isinstance(data, list) else data.get("data", [])
 
            markets = []
            for event in raw:
                for m in event.get("markets", []):
                    try:
                        outcomes = self._parse_outcomes(m)
                        if not outcomes:
                            continue
                        markets.append(Market(
                            condition_id=m.get("conditionId", m.get("id", "")),
                            question=m.get("question", event.get("title", "")),
                            category=event.get("category", "Other"),
                            volume=float(m.get("volume", 0) or 0),
                            liquidity=float(m.get("liquidity", 0) or 0),
                            end_date=m.get("endDate", event.get("endDate", "")),
                            outcomes=outcomes,
                            url=f"https://polymarket.com/event/{event.get('slug', '')}",
                        ))
                    except Exception:
                        continue
 
            log.info(f"Descargados {len(markets)} mercados via eventos")
            return markets
 
        except Exception as e:
            log.error(f"Error en fallback de eventos: {e}")
            return []
 
    def _parse_outcomes(self, m: dict) -> list[dict]:
        outcomes = []

        # Formato nuevo: outcomes/outcomePrices/clobTokenIds son strings JSON
        raw_names   = m.get("outcomes", "[]")
        raw_prices  = m.get("outcomePrices", "[]")
        raw_token_ids = m.get("clobTokenIds", "[]")
        if isinstance(raw_names, str):
            try:
                names     = json.loads(raw_names)
                prices    = json.loads(raw_prices)
                token_ids = json.loads(raw_token_ids)
                for i, name in enumerate(names):
                    try:
                        price = float(prices[i]) if i < len(prices) else 0
                        if 0.01 <= price <= 0.99:
                            outcomes.append({
                                "name": name,
                                "token_id": token_ids[i] if i < len(token_ids) else "",
                                "price": price,
                            })
                    except Exception:
                        continue
                if outcomes:
                    return outcomes
            except Exception:
                pass

        # Formato anterior: tokens es una lista de objetos
        for t in m.get("tokens", []):
            try:
                price = float(t.get("price", 0))
                if 0.01 <= price <= 0.99:
                    outcomes.append({
                        "name": t.get("outcome", ""),
                        "token_id": t.get("tokenId", ""),
                        "price": price,
                    })
            except Exception:
                continue
        return outcomes
 
    def filter_markets(self, markets: list[Market]) -> list[Market]:
        from datetime import datetime
        filtered = []
        now = datetime.utcnow()
        for m in markets:
            if m.liquidity < CONFIG["MIN_LIQUIDITY"]: continue
            if m.volume < CONFIG["MIN_VOLUME"]: continue
            try:
                end = datetime.fromisoformat(m.end_date.replace("Z", ""))
                days_left = (end - now).days
                if days_left < 2 or days_left > 90: continue
            except Exception:
                continue

            # Fase 1a: Rango de precios — descartar si todos los outcomes son extremos
            # Mercados a 0.95/0.05 ya están decididos, hay poco edge
            prices = [o["price"] for o in m.outcomes]
            if not any(0.15 <= p <= 0.85 for p in prices):
                continue

            # Fase 1b: Integridad binaria — en mercados Yes/No los precios deben sumar ~1.0
            if len(m.outcomes) == 2:
                price_sum = sum(o["price"] for o in m.outcomes)
                if abs(price_sum - 1.0) > 0.05:
                    continue

            filtered.append(m)
        log.info(f"Mercados tras filtro fase 1: {len(filtered)}")
        return filtered

    def deduplicate_markets(self, markets: list[Market]) -> list[Market]:
        """Elimina mercados que son variaciones del mismo evento (ej: 'X by April 15' y 'X by April 30')."""
        seen_topics = []
        unique = []

        for m in markets:
            # Extraer el tema base quitando fechas y thresholds numéricos
            topic = m.question.lower()
            topic = re.sub(r'\bby\s+\w+\s*\d*\b', '', topic)
            topic = re.sub(r'\bin\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b', '', topic)
            topic = re.sub(r'\bin\s+\d{4}\b', '', topic)
            topic = re.sub(r'\$[\d,]+', '', topic)
            topic = re.sub(r'\d+%', '', topic)
            topic = topic.strip()

            is_duplicate = False
            topic_words = set(topic.split())
            if len(topic_words) >= 3:
                for seen in seen_topics:
                    seen_words = set(seen.split())
                    overlap = len(topic_words & seen_words) / max(len(topic_words), len(seen_words))
                    if overlap > 0.70:  # 70% de palabras en común = probable duplicado
                        is_duplicate = True
                        break

            if not is_duplicate:
                seen_topics.append(topic)
                unique.append(m)

        if len(unique) < len(markets):
            log.info(f"Deduplicación: {len(markets)} → {len(unique)} mercados únicos")
        return unique

    def get_token_price(self, token_id: str) -> Optional[float]:
        """Obtiene el precio actual de un token usando el midpoint del order book."""
        try:
            resp = requests.get(
                f"{CLOB_API}/midpoint",
                params={"token_id": token_id},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            price = float(data.get("mid", 0))
            if 0 < price < 1:
                return price
        except Exception:
            pass
        # Fallback: last-trade-price si el midpoint falla
        try:
            resp = requests.get(
                f"{CLOB_API}/last-trade-price",
                params={"token_id": token_id},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            price = float(data.get("price", 0))
            return price if 0 < price < 1 else None
        except Exception:
            return None

    def find_late_resolution(self, limit=200) -> list[Market]:
        """
        Busca mercados donde la fecha de resolución ya pasó pero siguen activos.
        Filtros estrictos: solo mercados recientes, líquidos y de fácil resolución.
        Evita elecciones en países inestables, conflictos, golpes de estado, etc.
        """
        from datetime import datetime, timedelta
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "limit": limit, "order": "endDate", "ascending": "true"},
                timeout=20,
                headers={"Accept": "application/json"}
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data if isinstance(data, list) else data.get("data", [])
        except Exception as e:
            log.error(f"Error buscando late resolution: {e}")
            return []

        now = datetime.utcnow()
        opportunities = []

        # Palabras clave de mercados problemáticos (resolución lenta/incierta)
        risky_keywords = [
            "election", "presidente", "president", "war", "conflict",
            "coup", "military", "invasion", "regime", "parliament",
            "prime minister", "governor", "senator", "congressional",
            "guinea", "bissau", "venezuela", "myanmar", "sudan",
            "assassination", "impeach",
        ]

        for m in raw:
            try:
                end_date = m.get("endDate", "")
                if not end_date:
                    continue
                end = datetime.fromisoformat(end_date.replace("Z", ""))

                # Filtro 1: Solo mercados que ya vencieron
                if end > now:
                    continue

                # Filtro 2: Vencidos hace máximo 7 días
                if (now - end).days > 7:
                    continue

                # Filtro 3: Liquidez mínima $5,000
                liquidity = float(m.get("liquidity", 0) or 0)
                if liquidity < 5000:
                    continue

                # Filtro 4: Volumen 24h > $0 (mercado activo, no abandonado)
                volume_24h = float(m.get("volume24hr", 0) or 0)
                if volume_24h == 0:
                    continue

                # Filtro 5: Evitar mercados de difícil/lenta resolución
                question_lower = m.get("question", "").lower()
                if any(kw in question_lower for kw in risky_keywords):
                    log.debug(f"Late resolution descartado (risky): {question_lower[:60]}")
                    continue

                outcomes = self._parse_outcomes(m)
                if not outcomes:
                    continue

                # Filtro 6: Al menos un outcome con precio claro (>0.90, <0.99)
                for o in outcomes:
                    if 0.90 <= o["price"] <= 0.98:
                        opportunities.append(Market(
                            condition_id=m.get("conditionId", m.get("id", "")),
                            question=m.get("question", ""),
                            category="LATE_RESOLUTION",
                            volume=float(m.get("volume", 0) or 0),
                            liquidity=liquidity,
                            end_date=end_date,
                            outcomes=outcomes,
                            url=f"https://polymarket.com/event/{m.get('slug', '')}",
                        ))
                        break

            except Exception:
                continue

        log.info(f"Resolución tardía: {len(opportunities)} mercados encontrados (filtros estrictos)")
        return opportunities

    def find_correlated_arbitrage(self, markets: list[Market]) -> list[dict]:
        """
        Busca pares de mercados relacionados con precios inconsistentes.
        Ejemplo: 'X by April' a 40% y 'X by June' a 35% → el de junio está subvalorado.
        NO requiere Claude — detección matemática.
        """
        from datetime import datetime

        # Agrupar mercados por tema base (quitando fechas y números)
        groups: dict[str, list[Market]] = {}
        for m in markets:
            topic = m.question.lower()
            topic = re.sub(r'\bby\s+\w+\s*\d*\b', '', topic)
            topic = re.sub(r'\bin\s+(january|february|march|april|may|june|july|august'
                           r'|september|october|november|december)\b', '', topic)
            topic = re.sub(r'\$[\d,]+', '', topic)
            topic = re.sub(r'\d+%', '', topic)
            topic = re.sub(r'\d{4}', '', topic)
            topic = topic.strip()
            key_words = sorted(w for w in topic.split() if len(w) > 3)
            key = " ".join(key_words[:5])
            if not key:
                continue
            groups.setdefault(key, []).append(m)

        arb_opps = []

        for group in groups.values():
            if len(group) < 2:
                continue

            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    m1, m2 = group[i], group[j]
                    try:
                        end1 = datetime.fromisoformat(m1.end_date.replace("Z", ""))
                        end2 = datetime.fromisoformat(m2.end_date.replace("Z", ""))
                    except Exception:
                        continue

                    yes1 = next((o for o in m1.outcomes if o["name"].lower() == "yes"), None)
                    yes2 = next((o for o in m2.outcomes if o["name"].lower() == "yes"), None)
                    if not yes1 or not yes2:
                        continue

                    p1, p2 = yes1["price"], yes2["price"]

                    # Deadline más lejano DEBE tener precio >= deadline más cercano
                    if end2 > end1 and p2 < p1 - 0.05:
                        arb_opps.append({
                            "buy_market": m2, "buy_outcome": yes2,
                            "spread": p1 - p2,
                            "reasoning": (
                                f"'{m2.question[:50]}' ({p2:.1%}) debería ser >= "
                                f"'{m1.question[:50]}' ({p1:.1%}). Spread: {p1-p2:.1%}"
                            ),
                        })
                    elif end1 > end2 and p1 < p2 - 0.05:
                        arb_opps.append({
                            "buy_market": m1, "buy_outcome": yes1,
                            "spread": p2 - p1,
                            "reasoning": (
                                f"'{m1.question[:50]}' ({p1:.1%}) debería ser >= "
                                f"'{m2.question[:50]}' ({p2:.1%}). Spread: {p2-p1:.1%}"
                            ),
                        })

        arb_opps.sort(key=lambda x: x["spread"], reverse=True)
        if arb_opps:
            log.info(f"Arbitraje correlacionado: {len(arb_opps)} oportunidades encontradas")
        return arb_opps
 
    def get_market_by_condition(self, condition_id: str) -> Optional[dict]:
        """Obtiene datos actuales de un mercado específico."""
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets/{condition_id}",
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None
 
 
# ═══════════════════════════════════════════════════════════
#  CLAUDE ANALYZER
# ═══════════════════════════════════════════════════════════
class ClaudeAnalyzer:
 
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=CONFIG["ANTHROPIC_API_KEY"])
 
    def analyze_market(self, market: Market, bankroll: float) -> Optional[Opportunity]:
        """Analiza un mercado para detectar edge de entrada."""
        outcomes_str = "\n".join(
            f"  - {o['name']}: precio = {o['price']:.3f} ({o['price']*100:.1f}%)"
            for o in market.outcomes
        )
        prompt = f"""Eres un analista experto en mercados de predicción. Detecta si hay discrepancia entre el precio actual y la probabilidad REAL.
 
MERCADO:
Pregunta: "{market.question}"
Categoría: {market.category}
Liquidez: ${market.liquidity:,.0f} | Volumen: ${market.volume:,.0f}
Resolución: {market.end_date[:10]}
 
PRECIOS ACTUALES:
{outcomes_str}
 
1. Busca información reciente relevante en internet
2. Estima la probabilidad real de cada outcome
3. Calcula el edge = tu probabilidad - precio del mercado
 
RESPONDE SOLO EN JSON (sin texto adicional):
{{
  "analysis": "resumen en 2-3 oraciones",
  "has_edge": true/false,
  "best_outcome": "nombre del outcome",
  "best_outcome_token_id": "token_id",
  "market_price": 0.XX,
  "ai_probability": 0.XX,
  "edge": 0.XX,
  "confidence": "HIGH/MEDIUM/LOW",
  "reasoning": "explicación detallada del edge"
}}
 
Si el edge es menor a 5% o no tienes info suficiente: has_edge: false, edge: 0."""
 
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            text = "".join(b.text for b in response.content if b.type == "text")
            result = self._parse_json(text)
            if not result or not result.get("has_edge"):
                return None
 
            edge = float(result.get("edge", 0))
            if edge < CONFIG["MIN_EDGE"]:
                return None
 
            best_name  = result.get("best_outcome", "")
            best_token = result.get("best_outcome_token_id", "")
            outcome = next(
                (o for o in market.outcomes if o["name"] == best_name or o["token_id"] == best_token),
                market.outcomes[0]
            )
            ai_prob    = float(result.get("ai_probability", 0.5))
            mkt_price  = float(result.get("market_price", outcome["price"]))
            confidence = result.get("confidence", "LOW")
            kelly      = self._kelly(ai_prob, mkt_price) * 0.25
            bet        = self._size_bet(kelly, confidence, bankroll)
 
            if bet < CONFIG["MIN_BET_USD"]:
                return None
 
            return Opportunity(
                market=market,
                outcome_name=outcome["name"],
                token_id=outcome["token_id"],
                market_price=mkt_price,
                ai_probability=ai_prob,
                edge=edge,
                kelly_fraction=kelly,
                bet_size_usd=bet,
                reasoning=result.get("reasoning", result.get("analysis", "")),
                confidence=confidence,
            )
        except Exception as e:
            log.error(f"Error analizando '{market.question[:50]}': {e}")
            return None
 
    def should_close_position(self, position: Position, current_price: float) -> CloseDecision:
        """
        Claude decide si conviene cerrar una posición abierta.
        Se llama cuando las reglas mecánicas no son suficientes.
        """
        pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
        prompt = f"""Tienes una posición abierta en Polymarket. Decide si conviene CERRAR ahora o MANTENER.
 
POSICIÓN:
Mercado: "{position.market_question}"
Outcome apostado: {position.outcome}
Precio entrada: {position.entry_price:.3f}
Precio actual:  {current_price:.3f}
PnL actual:     {pnl_pct:+.1f}%
Probabilidad IA original: {position.ai_probability:.2f}
Fecha resolución: {position.end_date[:10]}
 
REGLA: Cierra si el mercado ya reflejó la información que te dio ventaja, o si hay nueva información negativa.
 
Busca noticias recientes sobre este mercado y decide.
 
RESPONDE SOLO EN JSON:
{{
  "should_close": true/false,
  "reason": "explicación de 1-2 oraciones",
  "updated_probability": 0.XX
}}"""
 
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            text = "".join(b.text for b in response.content if b.type == "text")
            result = self._parse_json(text)
            if result:
                pnl_usd = (current_price - position.entry_price) * position.shares
                return CloseDecision(
                    should_close=result.get("should_close", False),
                    reason=result.get("reason", ""),
                    current_price=current_price,
                    unrealized_pnl_usd=pnl_usd,
                    unrealized_pnl_pct=pnl_pct,
                )
        except Exception as e:
            log.error(f"Error en should_close_position: {e}")
 
        pnl_usd = (current_price - position.entry_price) * position.shares
        return CloseDecision(False, "Sin decisión de IA", current_price, pnl_usd, pnl_pct)
 
    def _parse_json(self, text: str) -> Optional[dict]:
        text = text.strip()
        start, end = text.find("{"), text.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        try:
            return json.loads(text[start:end])
        except Exception:
            return None
 
    def _kelly(self, prob: float, price: float) -> float:
        if price <= 0 or price >= 1: return 0
        b = (1.0 / price) - 1
        if b <= 0: return 0
        return max(0, (prob * b - (1 - prob)) / b)
 
    def _size_bet(self, kelly: float, confidence: str, bankroll: float) -> float:
        mult = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}.get(confidence, 0.4)
        max_by_pct = bankroll * CONFIG["MAX_BET_PCT"]
        raw = bankroll * kelly * mult
        return round(min(max(raw, CONFIG["MIN_BET_USD"]),
                         CONFIG["MAX_BET_USD"],
                         max_by_pct), 2)

    def pre_filter_markets(self, markets: list[Market]) -> list[Market]:
        """
        Fase 2: Claude identifica mercados ineficientes SIN web search (~$0.01).
        Usa criterios explícitos de edge real: info asimétrica, sesgo de mercado,
        precio irracional. Si no hay ninguno prometedor, devuelve [] y se salta
        el análisis profundo (ahorra ~$0.40).
        """
        if not markets:
            return []

        top_n = CONFIG["PRE_FILTER_TOP_N"]

        # Si hay pocos mercados, no vale la pena el llamado extra
        if len(markets) <= top_n:
            log.info(f"Pre-filtro fase 2: solo {len(markets)} mercados, saltando llamado batch")
            return markets

        # Construir resumen de cada mercado
        summaries = []
        for i, m in enumerate(markets):
            prices = ", ".join(f"{o['name']}={o['price']:.3f}" for o in m.outcomes)
            summaries.append(
                f"{i+1}. \"{m.question}\" | Cat: {m.category} | "
                f"Vol: ${m.volume:,.0f} | Liq: ${m.liquidity:,.0f} | "
                f"{prices} | Resuelve: {m.end_date[:10]}"
            )

        prompt = f"""Eres un trader experto en mercados de predicción buscando EDGE REAL — mercados donde el precio NO refleja la probabilidad verdadera.

MERCADOS DISPONIBLES:
{chr(10).join(summaries)}

CRITERIOS PARA SELECCIONAR (busca estos patrones):
1. INFORMACIÓN ASIMÉTRICA — ¿Sabes algo que el precio no refleja? (decisiones ya anunciadas, datos públicos ignorados, tendencias claras)
2. SESGO DE MERCADO — ¿El precio refleja opinión popular en vez de probabilidad real? (mercados emocionales, hype, miedo)
3. RESOLUCIÓN CLARA — ¿Puedes estimar la probabilidad real con confianza >70%?
4. PRECIO IRRACIONAL — ¿El precio está claramente fuera de rango lógico?

CRITERIOS PARA DESCARTAR:
- Deportes donde casas de apuestas ya tienen el precio correcto
- Mercados de "¿llegará X precio?" en crypto/commodities (traders pro dominan)
- Mercados donde genuinamente no tienes información para estimar mejor que el mercado
- Si no estás seguro, NO lo incluyas

Sé muy selectivo. Es mejor devolver 0-2 mercados realmente buenos que 5 mediocres.

RESPONDE SOLO EN JSON:
{{"promising": [1, 5], "reasoning": "breve explicación de por qué cada uno"}}

Si NINGUNO tiene edge real: {{"promising": [], "reasoning": "ninguno presenta oportunidad clara"}}"""

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
                # SIN tools= → sin web search → muy barato (~$0.01)
            )
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            result = self._parse_json(text)

            if not result or "promising" not in result:
                log.warning("Pre-filtro fase 2: respuesta no parseable, usando top por score")
                return markets[:top_n]

            indices = [int(x) - 1 for x in result["promising"]
                       if str(x).isdigit() and 0 < int(x) <= len(markets)]
            reasoning = result.get("reasoning", "")

            # Respetar decisión de Claude: si devuelve [], saltamos Phase 3 completamente
            if not indices:
                log.info(f"Pre-filtro fase 2: Claude no encontró edge real — saltando análisis profundo")
                if reasoning:
                    log.info(f"   Razón: {reasoning[:200]}")
                return []

            chosen = [markets[i] for i in indices]
            log.info(f"Pre-filtro fase 2: {len(markets)} → {len(chosen)} mercados prometedores (~$0.01)")
            if reasoning:
                log.info(f"   Razón: {reasoning[:200]}")
            return chosen

        except Exception as e:
            log.error(f"Error en pre_filter_markets: {e}")
            return markets[:top_n]

 
 
# ═══════════════════════════════════════════════════════════
#  POSITION MONITOR — decide cuándo cerrar
# ═══════════════════════════════════════════════════════════
class PositionMonitor:
    """
    Evalúa cada posición abierta y decide si cerrarla.
    Lógica en capas:
      1. Mercado resuelto → cerrar siempre
      2. Stop loss mecánico → cerrar
      3. Take profit mecánico → cerrar
      4. Pocos días restantes → cerrar para capturar liquidez
      5. Edge desapareció (precio corrigió) → Claude decide
    """
 
    def __init__(self, scanner: PolymarketScanner, analyzer: ClaudeAnalyzer):
        self.scanner  = scanner
        self.analyzer = analyzer
 
    def evaluate(self, position: Position) -> CloseDecision:
        from datetime import datetime
 
        # Precio actual
        current_price = self.scanner.get_token_price(position.token_id)
        if current_price is None:
            current_price = position.entry_price  # fallback
 
        pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
        pnl_usd = (current_price - position.entry_price) * position.shares
 
        # ── 1. Precio llegó a 0.95+ → mercado casi resuelto a favor ──────────
        if current_price >= 0.95:
            return CloseDecision(True, f"Mercado resuelto/casi resuelto a favor (precio={current_price:.3f})",
                                 current_price, pnl_usd, pnl_pct)
 
        # ── 2. Precio llegó a 0.05- → mercado casi resuelto en contra ────────
        if current_price <= 0.05:
            return CloseDecision(True, f"Mercado casi resuelto en contra (precio={current_price:.3f})",
                                 current_price, pnl_usd, pnl_pct)
 
        # ── 3. Stop loss mecánico ─────────────────────────────────────────────
        if pnl_pct <= -(CONFIG["STOP_LOSS_PCT"] * 100):
            return CloseDecision(True, f"Stop loss: {pnl_pct:.1f}% pérdida",
                                 current_price, pnl_usd, pnl_pct)
 
        # ── 4. Take profit mecánico ───────────────────────────────────────────
        if pnl_pct >= (CONFIG["TAKE_PROFIT_PCT"] * 100):
            return CloseDecision(True, f"Take profit: +{pnl_pct:.1f}% ganancia",
                                 current_price, pnl_usd, pnl_pct)
 
        # ── 5. Pocos días para resolución → cerrar para no quedar atascado ────
        try:
            end = datetime.fromisoformat(position.end_date.replace("Z", ""))
            days_left = (end - datetime.utcnow()).days
            if days_left <= CONFIG["CLOSE_DAYS_LEFT"] and pnl_pct > 0:
                return CloseDecision(True, f"Solo {days_left} días restantes, cerrando con ganancia",
                                     current_price, pnl_usd, pnl_pct)
        except Exception:
            pass
 
        # ── 6. Edge desapareció: precio subió a nuestro favor pero IA ya no ve más upside ──
        if CONFIG["CLOSE_IF_EDGE_GONE"] and pnl_pct > 10:
            # Precio ya subió bastante → preguntar a Claude si mantener o cerrar
            decision = self.analyzer.should_close_position(position, current_price)
            return decision
 
        return CloseDecision(False, "Mantener posición", current_price, pnl_usd, pnl_pct)
 
 
# ═══════════════════════════════════════════════════════════
#  EJECUTOR DE ÓRDENES
# ═══════════════════════════════════════════════════════════
class OrderExecutor:
 
    def __init__(self):
        self.clob = None
        if not CONFIG["DRY_RUN"] and CLOB_AVAILABLE and CONFIG["PRIVATE_KEY"]:
            try:
                proxy = CONFIG["PROXY_ADDRESS"] or None
                log.info(f"CLOB init: funder={'proxy wallet ' + proxy[:10] + '...' if proxy else 'None (EOA directo)'}")

                # Inicializar cliente con clave y proxy (proxy wallet mode)
                # signature_type=2 (POLY_GNOSIS_SAFE) para proxies Polymarket MetaMask
                # signature_type=None→0 (EOA) para modo directo sin proxy
                self.clob = ClobClient(
                    host=CLOB_API,
                    chain_id=POLYGON,
                    key=CONFIG["PRIVATE_KEY"],
                    funder=proxy,
                    signature_type=2 if proxy else None,
                )

                # Configurar credenciales de API
                if CONFIG["API_KEY"]:
                    self.clob.set_api_creds(ApiCreds(
                        api_key=CONFIG["API_KEY"],
                        api_secret=CONFIG["API_SECRET"],
                        api_passphrase=CONFIG["API_PASSPHRASE"],
                    ))
                    log.info("✅ CLOB conectado con credenciales del .env")
                else:
                    creds = None
                    try:
                        creds = self.clob.create_or_derive_api_key()
                        log.info("✅ Credenciales derivadas automáticamente")
                    except Exception as e1:
                        log.warning(f"create_or_derive_api_key falló: {e1}")
                        try:
                            creds = self.clob.derive_api_key()
                            log.info("✅ Credenciales derivadas con derive_api_key")
                        except Exception as e2:
                            log.warning(f"derive_api_key falló: {e2}")

                    if creds:
                        self.clob.set_api_creds(creds)
                        log.info(
                            f"💡 Agrega estas credenciales al .env para no regenerarlas:\n"
                            f"   POLYMARKET_API_KEY={getattr(creds, 'api_key', '?')}\n"
                            f"   POLYMARKET_API_SECRET={getattr(creds, 'api_secret', '?')}\n"
                            f"   POLYMARKET_API_PASSPHRASE={getattr(creds, 'api_passphrase', '?')}"
                        )
                    else:
                        log.error(
                            "❌ No se pudieron obtener credenciales de API.\n"
                            "   Agrega manualmente al .env:\n"
                            "     POLYMARKET_API_KEY=...\n"
                            "     POLYMARKET_API_SECRET=...\n"
                            "     POLYMARKET_API_PASSPHRASE=...\n"
                            "   Obtén las credenciales en: polymarket.com → Settings → API"
                        )
                        self.clob = None

            except Exception as e:
                log.error(f"Error CLOB: {e}")
                self.clob = None
        elif CONFIG["DRY_RUN"]:
            log.info("🧪 Modo DRY RUN — órdenes simuladas")

        # Cache de token_ids con exposición real en Polymarket (actualizado cada ciclo)
        self._live_token_ids: set[str] = set()

    def refresh_live_positions(self) -> None:
        """
        Consulta Polymarket (data API + CLOB open orders) para construir
        el conjunto de token_ids donde ya tenemos exposición real.
        Se llama una vez al inicio de cada ciclo.
        """
        token_ids: set[str] = set()

        # 1. Posiciones reales (tokens en cartera) vía data API pública
        address = CONFIG.get("PROXY_ADDRESS", "")
        if address and not CONFIG["DRY_RUN"]:
            try:
                resp = requests.get(
                    f"{POLYMARKET_DATA_API}/positions",
                    params={"user": address, "limit": 500},
                    timeout=8,
                    headers={
                        "Accept": "application/json",
                        "Origin": "https://polymarket.com",
                        "Referer": "https://polymarket.com/",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                items = data if isinstance(data, list) else data.get("data", [])
                for p in items:
                    size = float(p.get("size", p.get("amount", 0)) or 0)
                    if size > 0:
                        tid = p.get("asset", p.get("asset_id", p.get("token_id", "")))
                        if tid:
                            token_ids.add(str(tid))
                log.info(f"[CLOB] Posiciones reales en Polymarket: {len(token_ids)} tokens")
            except Exception as e:
                log.warning(f"[CLOB] No se pudo consultar posiciones: {e}")

        # 2. Órdenes pendientes (limit orders en espera) vía CLOB SDK
        if self.clob is not None and not CONFIG["DRY_RUN"]:
            try:
                orders = self.clob.get_orders()
                for o in (orders or []):
                    tid = (
                        getattr(o, "asset_id", None)
                        or getattr(o, "token_id", None)
                        or (o.get("asset_id") if isinstance(o, dict) else None)
                        or (o.get("token_id") if isinstance(o, dict) else None)
                    )
                    if tid:
                        token_ids.add(str(tid))
                log.info(f"[CLOB] Órdenes pendientes: {len(token_ids)} tokens (total con posiciones)")
            except Exception as e:
                log.warning(f"[CLOB] No se pudo consultar órdenes abiertas: {e}")

        self._live_token_ids = token_ids

    def has_market_exposure(self, token_ids: list[str]) -> bool:
        """
        Devuelve True si alguno de los token_ids dados ya tiene
        exposición real en Polymarket (posición o orden pendiente).
        """
        return bool(self._live_token_ids & set(token_ids))

    def buy(self, opp: Opportunity) -> Optional[Position]:
        """Ejecuta orden de compra y devuelve la posición creada."""
        import uuid
        # Exchange mínimo $5 USDC — ajustar si Kelly da menos
        bet_size = max(opp.bet_size_usd, 5.0)
        shares = bet_size / opp.market_price

        if CONFIG["DRY_RUN"]:
            log.info(
                f"[DRY RUN] 🟢 BUY '{opp.outcome_name}' @ {opp.market_price:.3f} "
                f"| ${bet_size} → {shares:.2f} shares "
                f"| Edge: +{opp.edge*100:.1f}% | Conf: {opp.confidence}"
            )
        elif self.clob is None:
            log.error("BUY abortado: CLOB no inicializado (revisa PRIVATE_KEY y conexión)")
            return None
        else:
            if bet_size > opp.bet_size_usd:
                log.info(f"Apuesta ${opp.bet_size_usd:.2f} ajustada al mínimo del exchange $5")
            try:
                order = OrderArgs(
                    token_id=opp.token_id,
                    price=round(opp.market_price + 0.001, 3),
                    size=bet_size,
                    side="BUY",
                )
                self.clob.create_and_post_order(order)
            except Exception as e:
                log.error(f"Error ejecutando BUY: {e}")
                return None
 
        return Position(
            id=str(uuid.uuid4())[:8],
            market_condition_id=opp.market.condition_id,
            market_question=opp.market.question,
            outcome=opp.outcome_name,
            token_id=opp.token_id,
            size_usd=bet_size,
            shares=shares,
            entry_price=opp.market_price,
            ai_probability=opp.ai_probability,
            end_date=opp.market.end_date,
            opened_at=datetime.now().isoformat(),
        )
 
    def sell(self, position: Position, current_price: float) -> float:
        """
        Cierra una posición (vende shares).
        Devuelve el monto recibido en USDC.
        """
        received = position.shares * current_price
        pnl_usd  = received - position.size_usd
        pnl_pct  = pnl_usd / position.size_usd * 100
 
        if CONFIG["DRY_RUN"]:
            log.info(
                f"[DRY RUN] {'🟢' if pnl_usd >= 0 else '🔴'} SELL '{position.outcome}' "
                f"@ {current_price:.3f} | Recibido: ${received:.2f} | "
                f"PnL: {'+'if pnl_usd>=0 else ''}{pnl_usd:.2f} ({pnl_pct:+.1f}%)"
            )
            return received
        elif self.clob is None:
            log.error("SELL abortado: CLOB no inicializado")
            return position.size_usd  # devolver lo invertido como fallback seguro
 
        try:
            order = OrderArgs(
                token_id=position.token_id,
                price=round(current_price - 0.001, 3),
                size=position.shares,
                side="SELL",
            )
            self.clob.create_and_post_order(order)
            return received
        except Exception as e:
            log.error(f"Error ejecutando SELL: {e}")
            return position.size_usd   # fallback: devolver lo invertido
 
 
# ═══════════════════════════════════════════════════════════
#  BANKROLL MANAGER — reinvierte ganancias
# ═══════════════════════════════════════════════════════════
class BankrollManager:
    """
    Actualiza el bankroll dinámicamente cuando se cierran posiciones.
    Las ganancias se reinvierten; las pérdidas se descuentan.
    Esto hace que el agente apueste más cuando va bien y menos cuando va mal.
    """
 
    def update(self, state: AgentState, received: float, position: Position) -> float:
        """
        Actualiza el bankroll con el resultado de una posición cerrada.
        Devuelve el PnL de esta operación.
        """
        pnl = received - position.size_usd
        old_bankroll = state.bankroll
 
        # Reintegrar lo recibido al bankroll
        state.bankroll += received
 
        # Actualizar métricas
        state.daily_pnl  += pnl
        state.total_invested += position.size_usd
        state.total_returned += received
 
        log.info(
            f"💰 BANKROLL: ${old_bankroll:.2f} → ${state.bankroll:.2f} "
            f"(PnL: {'+'if pnl>=0 else ''}{pnl:.2f} | Total: {state.total_pnl_pct:+.1f}%)"
        )
        return pnl
 
 
# ═══════════════════════════════════════════════════════════
#  ESTADO PERSISTENTE
# ═══════════════════════════════════════════════════════════
class StatePersistence:
    """Guarda y carga el estado del agente en disco (JSON)."""
 
    @staticmethod
    def save(state: AgentState):
        data = {
            "bankroll": state.bankroll,
            "initial_bankroll": state.initial_bankroll,
            "daily_pnl": state.daily_pnl,
            "daily_loss_triggered": state.daily_loss_triggered,
            "total_invested": state.total_invested,
            "total_returned": state.total_returned,
            "session_start": state.session_start,
            "analyzed_today": list(state.analyzed_today),
            "open_positions": [vars(p) for p in state.open_positions],
            "closed_positions": [vars(p) for p in state.closed_positions[-100:]],  # últimas 100
        }
        with open(CONFIG["STATE_FILE"], "w") as f:
            json.dump(data, f, indent=2)
 
    @staticmethod
    def load() -> Optional[AgentState]:
        if not os.path.exists(CONFIG["STATE_FILE"]):
            return None
        try:
            with open(CONFIG["STATE_FILE"]) as f:
                data = json.load(f)
            state = AgentState(
                bankroll=data["bankroll"],
                initial_bankroll=data["initial_bankroll"],
            )
            state.daily_pnl = data.get("daily_pnl", 0)
            state.daily_loss_triggered = data.get("daily_loss_triggered", False)
            state.total_invested = data.get("total_invested", 0)
            state.total_returned = data.get("total_returned", 0)
            state.session_start = data.get("session_start", datetime.now().isoformat())
            state.analyzed_today = set(data.get("analyzed_today", []))
            state.open_positions = [Position(**p) for p in data.get("open_positions", [])]
            state.closed_positions = [Position(**p) for p in data.get("closed_positions", [])]
            log.info(f"Estado cargado: bankroll=${state.bankroll:.2f} | {len(state.open_positions)} posiciones abiertas")
            return state
        except Exception as e:
            log.error(f"Error cargando estado: {e}")
            return None
 
 
# ═══════════════════════════════════════════════════════════
#  SMART MONEY MONITOR — copytrading de wallets verificadas
# ═══════════════════════════════════════════════════════════
class SmartMoneyMonitor:

    def __init__(self, scanner: "PolymarketScanner", executor: "OrderExecutor", state: "AgentState"):
        self.scanner  = scanner
        self.executor = executor
        self.state    = state

    def fetch_recent_trades(self, wallet_address: str, limit: int = 20) -> list[dict]:
        try:
            resp = requests.get(
                f"{POLYMARKET_DATA_API}/trades",
                params={"user": wallet_address, "limit": limit},
                timeout=10,
                headers={
                    "Accept": "application/json",
                    "Origin": "https://polymarket.com",
                    "Referer": "https://polymarket.com/",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data.get("data", [])
            return data if isinstance(data, list) else []
        except Exception as e:
            log.error(f"[SmartMoney] Error fetching {wallet_address[:10]}: {e}")
            return []

    def is_trade_fresh(self, trade: dict) -> bool:
        from datetime import timezone, timedelta
        ts = trade.get("timestamp")
        if not ts:
            return False
        try:
            if isinstance(ts, (int, float)):
                trade_time = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                trade_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return datetime.now(timezone.utc) - trade_time < timedelta(hours=CONFIG["SMART_MONEY_MAX_HOURS"])
        except Exception:
            return False

    def get_token_id(self, trade: dict) -> Optional[str]:
        for key in ("asset", "tokenId", "token_id", "asset_id"):
            if trade.get(key):
                return str(trade[key])
        return None

    def get_condition_id(self, trade: dict) -> Optional[str]:
        # conditionId / condition_id son siempre hex válidos
        for key in ("conditionId", "condition_id"):
            val = trade.get(key)
            if val:
                return str(val)
        # "market" puede ser slug de mercado o hex — solo usar si parece hex
        market_val = trade.get("market")
        if market_val and str(market_val).startswith("0x") and len(str(market_val)) > 10:
            return str(market_val)
        return None

    _CRYPTO_PRICE_KEYWORDS = frozenset([
        "bitcoin", "btc", "ethereum", "eth", "crypto", "price", "solana", "sol",
        "xrp", "ripple", "doge", "dogecoin", "bnb", "usdc", "usdt", "stablecoin",
        "altcoin", "defi", "nft", "token", "coin",
    ])

    @classmethod
    def _is_crypto_price_market(cls, question: str) -> bool:
        q = question.lower()
        return any(kw in q for kw in cls._CRYPTO_PRICE_KEYWORDS)

    def should_copy(self, trade: dict, wallet_name: str) -> bool:
        question = str(trade.get("title", trade.get("question", "")))
        if self._is_crypto_price_market(question):
            log.info(f"[SmartMoney] {wallet_name}: excluido por pregunta crypto/precio — '{question[:60]}'")
            return False
        side = str(trade.get("side", "")).upper()
        if side != "BUY":
            log.info(f"[SmartMoney] {wallet_name}: descartado por side='{side}' (keys: {list(trade.keys())})")
            return False
        ts = trade.get("timestamp")
        if not self.is_trade_fresh(trade):
            log.info(f"[SmartMoney] {wallet_name}: trade no fresco (timestamp={ts!r})")
            return False
        try:
            price = float(trade.get("price", 0))
        except (ValueError, TypeError):
            log.info(f"[SmartMoney] {wallet_name}: precio inválido ({trade.get('price')!r})")
            return False
        if not (0.15 <= price <= 0.85):
            log.info(f"[SmartMoney] {wallet_name}: precio fuera de zona ({price:.2f})")
            return False
        condition_id = self.get_condition_id(trade)
        if not condition_id:
            log.info(f"[SmartMoney] {wallet_name}: sin condition_id (keys: {list(trade.keys())})")
            return False
        if condition_id in self.state.analyzed_today:
            log.info(f"[SmartMoney] {wallet_name}: condition_id ya analizado hoy")
            return False
        if any(p.market_condition_id == condition_id for p in self.state.open_positions):
            log.info(f"[SmartMoney] {wallet_name}: posición ya abierta en este mercado (condition_id)")
            return False
        # Check adicional por token_id para capturar mismatches de condition_id
        token_id_check = self.get_token_id(trade)
        if token_id_check and any(p.token_id == token_id_check for p in self.state.open_positions):
            log.info(f"[SmartMoney] {wallet_name}: token_id ya en posición local — saltando")
            return False
        end_date_raw = trade.get("endDate") or trade.get("end_date")
        if end_date_raw:
            try:
                from datetime import timezone
                end_dt = datetime.fromisoformat(str(end_date_raw).replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                if end_dt <= datetime.now(timezone.utc):
                    log.info(f"[SmartMoney] {wallet_name}: mercado ya vencido (end_date={end_date_raw})")
                    return False
            except (ValueError, TypeError):
                pass
        # Guard por question normalizada: detecta duplicados aunque condition_id difiera entre APIs
        question = str(trade.get("title", trade.get("question", "")))
        norm_q = question.lower().strip()
        if norm_q and any(p.market_question.lower().strip() == norm_q for p in self.state.open_positions):
            log.info(f"[SmartMoney] {wallet_name}: pregunta ya en posición abierta — '{question[:60]}'")
            return False
        return True

    def copy_trade(self, trade: dict, wallet_name: str) -> Optional[Position]:
        token_id     = self.get_token_id(trade)
        condition_id = self.get_condition_id(trade)
        if not token_id or not condition_id:
            return None

        # Guard final antes de ejecutar: chequeo local por condition_id Y token_id,
        # más verificación en Polymarket real para evitar posiciones contradictorias.
        if condition_id in self.state.analyzed_today:
            log.info(f"[SmartMoney] {wallet_name}: condition_id en analyzed_today — saltando")
            return None
        if any(p.market_condition_id == condition_id for p in self.state.open_positions):
            log.info(f"[SmartMoney] {wallet_name}: posición local por condition_id — saltando")
            return None
        if any(p.token_id == token_id for p in self.state.open_positions):
            log.info(f"[SmartMoney] {wallet_name}: token_id ya en posición local — saltando")
            self.state.analyzed_today.add(condition_id)
            return None
        if self.executor.has_market_exposure([token_id]):
            log.warning(f"[SmartMoney] {wallet_name}: exposición real en Polymarket — saltando")
            self.state.analyzed_today.add(condition_id)
            return None

        original_price = float(trade.get("price", 0))
        current_price  = self.scanner.get_token_price(token_id)
        if current_price is None:
            log.info(f"[SmartMoney] {wallet_name}: no se pudo obtener precio actual (token_id={token_id[:10]}...)")
            return None

        slippage = (current_price - original_price) / max(original_price, 1e-9)
        if slippage > CONFIG["SMART_MONEY_MAX_SLIPPAGE"]:
            log.info(
                f"[SmartMoney] {wallet_name}: slippage {slippage*100:.1f}% > "
                f"{CONFIG['SMART_MONEY_MAX_SLIPPAGE']*100:.0f}% (compró a {original_price:.3f}, ahora {current_price:.3f})"
            )
            return None
        if not (0.15 <= current_price <= 0.85):
            log.info(f"[SmartMoney] {wallet_name}: precio actual fuera de zona ({current_price:.3f})")
            return None

        bet_size = round(
            max(CONFIG["MIN_BET_USD"],
                min(CONFIG["MAX_BET_USD"],
                    self.state.bankroll * CONFIG["SMART_MONEY_BET_PCT"])),
            2
        )
        if self.state.bankroll < bet_size + 5:
            log.warning("[SmartMoney] Bankroll insuficiente")
            return None

        market = Market(
            condition_id=condition_id,
            question=str(trade.get("title", trade.get("question", "Smart Money Copy"))),
            category="SMART_MONEY",
            volume=0,
            liquidity=0,
            end_date=str(trade.get("endDate", "")),
            outcomes=[{"name": str(trade.get("outcome", "Yes")),
                       "token_id": token_id, "price": current_price}],
            url="",
        )
        opp = Opportunity(
            market=market,
            outcome_name=market.outcomes[0]["name"],
            token_id=token_id,
            market_price=current_price,
            ai_probability=min(0.99, current_price + 0.10),
            edge=0.10,
            kelly_fraction=CONFIG["SMART_MONEY_BET_PCT"],
            bet_size_usd=bet_size,
            reasoning=f"Smart Money copy de {wallet_name}",
            confidence="MEDIUM",
        )
        log.info(
            f"[SmartMoney] 🐋 COPIANDO {wallet_name}: '{market.question[:50]}' | "
            f"{opp.outcome_name} @ {current_price:.3f} | ${bet_size:.2f}"
        )
        try:
            chk = requests.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
                timeout=8,
            )
            body = chk.json()
            if isinstance(body, dict) and "orderbook does not exist" in str(body.get("error", "")).lower():
                log.info(
                    f"[SmartMoney] {wallet_name}: orderbook no existe para "
                    f"'{market.question[:40]}', blacklistando condition_id"
                )
                self.state.analyzed_today.add(condition_id)
                return None
        except Exception:
            pass
        return self.executor.buy(opp)

    def run(self) -> int:
        if not CONFIG["SMART_MONEY_ENABLED"]:
            return 0

        slots = CONFIG["MAX_OPEN_BETS"] - len(self.state.open_positions)
        if slots <= 0:
            log.info("[SmartMoney] Posiciones al máximo, saltando")
            return 0

        copies_made = 0
        max_copies  = min(CONFIG["SMART_MONEY_MAX_COPIES"], slots)
        log.info(f"[SmartMoney] Monitoreando {len(SMART_WALLETS)} wallets...")

        for wallet_info in SMART_WALLETS:
            if copies_made >= max_copies:
                break
            address = wallet_info["address"]
            name    = wallet_info["name"]
            trades  = self.fetch_recent_trades(address)

            if not trades:
                log.info(f"[SmartMoney] {name}: sin trades recientes")
                continue
            log.info(f"[SmartMoney] {name}: {len(trades)} trades encontrados")
            log.info(f"[SmartMoney] {name} sample trade keys: {list(trades[0].keys())}")
            log.info(f"[SmartMoney] {name} sample trade: side={trades[0].get('side')!r} price={trades[0].get('price')!r} timestamp={trades[0].get('timestamp')!r}")

            for trade in trades:
                if copies_made >= max_copies:
                    break
                if not self.should_copy(trade, name):
                    continue
                condition_id = self.get_condition_id(trade)
                position = self.copy_trade(trade, name)
                if position:
                    self.state.bankroll -= position.size_usd
                    self.state.open_positions.append(position)
                    if condition_id:
                        self.state.analyzed_today.add(condition_id)
                    log.info("[SmartMoney] ✅ Posición de copia abierta")
                    copies_made += 1

        log.info(f"[SmartMoney] Ciclo terminado: {copies_made} copias")
        return copies_made


# ═══════════════════════════════════════════════════════════
#  CONTRARIAN FADE — apuesta contra movimientos exagerados
# ═══════════════════════════════════════════════════════════
class ContrarianFadeStrategy:
    """
    Detecta tokens con movimiento >10% en 24h y apuesta CONTRA.

    Lógica:
    - Si "Yes" subió >10% → comprar "No" (asumiendo sobrerreacción al alza)
    - Si "Yes" bajó >10% → comprar "Yes" (asumiendo sobrerreacción a la baja)

    Filtros:
    - Precio actual entre 0.30-0.70 (zona de incertidumbre)
    - Movimiento entre 10% y 40% (>40% probablemente es noticia real)
    - Mínimo 2 días para resolución
    - Apuesta fija de $2 (confianza BAJA, no escalar con Kelly)

    NO usa Claude API. Es 100% gratis.
    Reutiliza la lista de mercados ya descargada por _run_cycle.
    """

    def __init__(self, scanner: PolymarketScanner, executor: OrderExecutor, state: AgentState):
        self.scanner = scanner
        self.executor = executor
        self.state = state

    def get_price_change(self, token_id: str) -> Optional[float]:
        """
        Obtiene el cambio porcentual del precio en las últimas N horas
        usando el endpoint clob.polymarket.com/prices-history.

        Retorna: float (ej: 0.15 = +15%, -0.20 = -20%) o None si falla.
        """
        try:
            end = int(time.time())
            lookback_seconds = CONFIG["CONTRARIAN_LOOKBACK_HOURS"] * 3600
            start = end - lookback_seconds

            resp = requests.get(
                CLOB_PRICES_HISTORY,
                params={
                    "market": token_id,
                    "startTs": start,
                    "endTs": end,
                    "fidelity": 60,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            history = data.get("history", [])

            if len(history) < 2:
                return None

            old_price = float(history[0]["p"])
            new_price = float(history[-1]["p"])

            if old_price <= 0:
                return None

            return (new_price - old_price) / old_price

        except Exception as e:
            log.debug(f"[Contrarian] Error obteniendo precio histórico: {e}")
            return None

    def find_outcome_by_name(self, market: Market, name: str) -> Optional[dict]:
        """Busca un outcome por nombre (case-insensitive)."""
        for o in market.outcomes:
            if o["name"].lower() == name.lower():
                return o
        return None

    _CRYPTO_PRICE_KEYWORDS = SmartMoneyMonitor._CRYPTO_PRICE_KEYWORDS

    def evaluate_market(self, market: Market) -> Optional[dict]:
        """
        Evalúa si vale la pena hacer fade en este mercado.
        Retorna dict con info del trade o None si no aplica.
        """
        # Filtro 0: excluir mercados de precio crypto (contradiciones imposibles de gestionar)
        if any(kw in market.question.lower() for kw in self._CRYPTO_PRICE_KEYWORDS):
            return None

        # Filtro 1: días para resolución
        try:
            end_dt = datetime.fromisoformat(market.end_date.replace("Z", ""))
            days_left = (end_dt - datetime.utcnow()).days
            if days_left < CONFIG["CONTRARIAN_MIN_DAYS_LEFT"]:
                return None
        except Exception:
            return None

        # Filtro 2: necesita outcomes (Yes/No)
        if len(market.outcomes) < 2:
            return None

        yes_outcome = self.find_outcome_by_name(market, "Yes")
        no_outcome = self.find_outcome_by_name(market, "No")
        if not yes_outcome or not no_outcome:
            return None

        current_yes_price = yes_outcome["price"]

        # Filtro 3: precio en zona de incertidumbre
        if not (CONFIG["CONTRARIAN_PRICE_MIN"] <= current_yes_price <= CONFIG["CONTRARIAN_PRICE_MAX"]):
            return None

        # Calcular cambio en 24h del Yes
        change = self.get_price_change(yes_outcome["token_id"])
        if change is None:
            return None

        abs_change = abs(change)

        # Filtro 4: movimiento dentro del rango target
        if abs_change < CONFIG["CONTRARIAN_MIN_MOVE"]:
            return None
        if abs_change > CONFIG["CONTRARIAN_MAX_MOVE"]:
            return None

        # Decidir dirección del fade
        if change > 0:
            target = no_outcome
            direction_log = "Yes ↑ → comprar No"
        else:
            target = yes_outcome
            direction_log = "Yes ↓ → comprar Yes"

        return {
            "outcome": target,
            "yes_change": change,
            "yes_price_now": current_yes_price,
            "direction_log": direction_log,
        }

    def execute_fade(self, market: Market, fade_info: dict) -> bool:
        """Ejecuta el fade construyendo Opportunity y usando OrderExecutor.buy."""
        outcome = fade_info["outcome"]
        bet_size = CONFIG["CONTRARIAN_BET_USD"]

        # Verificar bankroll
        if self.state.bankroll < bet_size + 5:
            log.warning(f"[Contrarian] Bankroll insuficiente para fade")
            return False

        # Verificar slots
        if len(self.state.open_positions) >= CONFIG["MAX_OPEN_BETS"]:
            return False

        # Guard 1: condition_id ya analizado o ya en posiciones locales
        if market.condition_id in self.state.analyzed_today:
            log.info(f"[Contrarian] condition_id ya en analyzed_today — saltando")
            return False
        if any(p.market_condition_id == market.condition_id for p in self.state.open_positions):
            log.info(f"[Contrarian] Ya hay posición local para este condition_id — saltando")
            return False

        # Guard 2: verificar por token_id en posiciones locales (más fiable que condition_id)
        all_token_ids = [o["token_id"] for o in market.outcomes if o.get("token_id")]
        if any(p.token_id in all_token_ids for p in self.state.open_positions):
            log.info(f"[Contrarian] token_id ya en posición local — saltando")
            self.state.analyzed_today.add(market.condition_id)
            return False

        # Guard 2b: verificar por question normalizada — detecta duplicados cross-strategy
        norm_q = market.question.lower().strip()
        if any(p.market_question.lower().strip() == norm_q for p in self.state.open_positions):
            log.info(f"[Contrarian] Pregunta ya en posición abierta — saltando '{market.question[:60]}'")
            self.state.analyzed_today.add(market.condition_id)
            return False

        # Guard 3: verificar en Polymarket real (posición o orden pendiente)
        if self.executor.has_market_exposure(all_token_ids):
            log.warning(f"[Contrarian] Exposición real en Polymarket detectada — saltando '{market.question[:50]}'")
            self.state.analyzed_today.add(market.condition_id)
            return False

        # Verificar precio mínimo válido
        if outcome["price"] <= 0 or outcome["price"] >= 1:
            return False

        opp = Opportunity(
            market=market,
            outcome_name=outcome["name"],
            token_id=outcome["token_id"],
            market_price=outcome["price"],
            ai_probability=outcome["price"] + 0.10,
            edge=0.10,
            kelly_fraction=bet_size / max(self.state.bankroll, 1),
            bet_size_usd=bet_size,
            reasoning=f"Contrarian fade: Yes cambió {fade_info['yes_change']*100:+.1f}% en 24h",
            confidence="LOW",
        )

        log.info(
            f"[Contrarian] FADE: '{market.question[:60]}' | "
            f"{fade_info['direction_log']} @ {outcome['price']:.3f} | "
            f"Cambio Yes 24h: {fade_info['yes_change']*100:+.1f}% | ${bet_size}"
        )

        position = self.executor.buy(opp)
        if position:
            self.state.bankroll -= bet_size
            self.state.open_positions.append(position)
            self.state.analyzed_today.add(market.condition_id)
            log.info(f"[Contrarian] Posicion de fade abierta")
            return True
        return False

    def run(self, markets: list[Market]) -> int:
        """
        Ejecuta un ciclo de búsqueda de fades.
        Recibe la lista de mercados ya descargada por _run_cycle (no hace fetch propio).
        Retorna número de fades ejecutados.
        """
        if not CONFIG["CONTRARIAN_ENABLED"]:
            return 0

        if not markets:
            return 0

        slots_disponibles = CONFIG["MAX_OPEN_BETS"] - len(self.state.open_positions)
        if slots_disponibles <= 0:
            log.info("[Contrarian] Posiciones al máximo, saltando")
            return 0

        max_fades = min(CONFIG["CONTRARIAN_MAX_POSITIONS"], slots_disponibles)
        fades_ejecutados = 0
        analizados = 0
        max_scan = CONFIG["CONTRARIAN_MAX_MARKETS_TO_SCAN"]

        log.info(f"[Contrarian] Buscando fades en hasta {max_scan} mercados...")

        for market in markets[:max_scan]:
            if fades_ejecutados >= max_fades:
                break

            analizados += 1
            fade_info = self.evaluate_market(market)
            if not fade_info:
                continue

            if self.execute_fade(market, fade_info):
                fades_ejecutados += 1

        log.info(f"[Contrarian] Ciclo terminado: {analizados} analizados, {fades_ejecutados} fades")
        return fades_ejecutados


# ═══════════════════════════════════════════════════════════
#  AGENTE PRINCIPAL
# ═══════════════════════════════════════════════════════════
class PolymarketAgent:
 
    def __init__(self):
        self.scanner   = PolymarketScanner()
        self.analyzer  = ClaudeAnalyzer()
        self.monitor   = PositionMonitor(self.scanner, self.analyzer)
        self.executor  = OrderExecutor()
        self.bankroll_mgr = BankrollManager()
 
        # Cargar estado previo o crear nuevo
        saved = StatePersistence.load()
        if saved:
            self.state = saved
            log.info("✅ Estado previo restaurado")
        else:
            self.state = AgentState(
                bankroll=CONFIG["BANKROLL"],
                initial_bankroll=CONFIG["BANKROLL"],
            )
            log.info(f"🆕 Nuevo agente iniciado con ${CONFIG['BANKROLL']} USDC")

        self.smart_money = SmartMoneyMonitor(self.scanner, self.executor, self.state)
        self.contrarian = ContrarianFadeStrategy(self.scanner, self.executor, self.state)
        self._print_banner()
 
    def run(self):
        log.info("Agente iniciado. Ctrl+C para detener.")
        try:
            while True:
                self._run_cycle()
                StatePersistence.save(self.state)
                log.info(f"Próximo ciclo en {CONFIG['SCAN_INTERVAL_MIN']} min...")
                time.sleep(CONFIG["SCAN_INTERVAL_MIN"] * 60)
        except KeyboardInterrupt:
            log.info("Detenido manualmente.")
            StatePersistence.save(self.state)
            self._print_summary()
 
    def _run_cycle(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info(f"═══ CICLO {now} | Bankroll: ${self.state.bankroll:.2f} ═══")

        if not CONFIG["DRY_RUN"] and self.executor.clob is None:
            log.error(
                "⛔ CLOB no inicializado — revisa credenciales en .env. "
                "Ciclo cancelado para no gastar créditos de Claude."
            )
            return

        # ── FASE 0: Refrescar posiciones/órdenes reales de Polymarket ────────
        # Esto llena executor._live_token_ids para los guards de execute_fade / copy_trade
        self.executor.refresh_live_positions()

        # ── FASE 1: Resetear stop diario si es nuevo día ─────────────────────
        self._check_daily_reset()
 
        if self.state.daily_loss_triggered:
            log.warning("⛔ Stop diario activo. Saltando ciclo.")
            return
 
        # ── FASE 2: Monitorear y cerrar posiciones abiertas ───────────────────
        self._monitor_positions()
 
        # ── FASE 3: Buscar nuevas oportunidades ────────────────────────────────
        slots = CONFIG["MAX_OPEN_BETS"] - len(self.state.open_positions)
        if slots <= 0:
            log.info("Posiciones al máximo. Solo monitoreando.")
            return

        # ── ESTRATEGIA A: Resolución tardía (GRATIS — sin Claude) ────────────
        # No usa analyzed_today: mercados pueden permanecer sin resolver varios días,
        # y necesitamos poder reentrar si la posición anterior ya se cerró.
        late_markets = self.scanner.find_late_resolution()
        for m in late_markets[:3]:
            if slots <= 0:
                break
            # Solo bloquear si ya tenemos posición ABIERTA en este mercado
            if any(p.market_condition_id == m.condition_id for p in self.state.open_positions):
                continue
            best = max(m.outcomes, key=lambda o: o["price"])
            if best["price"] < 0.90:
                continue
            bet_size = max(CONFIG["MIN_BET_USD"], min(3.0, self.state.bankroll * 0.05))
            if self.state.bankroll < bet_size + 5:
                continue
            expected_profit_pct = ((1.0 / best["price"]) - 1) * 100
            log.info(
                f"LATE RESOLUTION: '{m.question[:60]}' | "
                f"{best['name']} @ {best['price']:.3f} | "
                f"Ganancia esperada: +{expected_profit_pct:.1f}% | Apuesta: ${bet_size:.2f}"
            )
            opp = Opportunity(
                market=m,
                outcome_name=best["name"],
                token_id=best["token_id"],
                market_price=best["price"],
                ai_probability=0.95,
                edge=round(1.0 - best["price"], 4),
                kelly_fraction=0.05,
                bet_size_usd=bet_size,
                reasoning=f"Late resolution: mercado ya venció, {best['name']} a {best['price']:.3f}",
                confidence="HIGH",
            )
            position = self.executor.buy(opp)
            if position:
                self.state.bankroll -= bet_size
                self.state.open_positions.append(position)
                slots -= 1
                log.info("  Posición de resolución tardía abierta")

        # ── ESTRATEGIA B: Arbitraje correlacionado (GRATIS — sin Claude) ─────
        all_markets_raw = self.scanner.get_active_markets()
        all_markets_filtered = self.scanner.filter_markets(all_markets_raw)
        arb_opps = self.scanner.find_correlated_arbitrage(all_markets_filtered)
        for arb in arb_opps[:2]:
            if slots <= 0:
                break
            buy_m = arb["buy_market"]
            buy_o = arb["buy_outcome"]
            if buy_m.condition_id in self.state.analyzed_today:
                continue
            if any(p.market_condition_id == buy_m.condition_id for p in self.state.open_positions):
                continue
            bet_size = max(CONFIG["MIN_BET_USD"], min(3.0, self.state.bankroll * 0.05))
            if self.state.bankroll < bet_size + 5:
                continue
            log.info(f"ARBITRAJE: {arb['reasoning']}")
            opp = Opportunity(
                market=buy_m,
                outcome_name=buy_o["name"],
                token_id=buy_o["token_id"],
                market_price=buy_o["price"],
                ai_probability=min(0.99, buy_o["price"] + arb["spread"]),
                edge=arb["spread"],
                kelly_fraction=0.05,
                bet_size_usd=bet_size,
                reasoning=arb["reasoning"],
                confidence="HIGH",
            )
            position = self.executor.buy(opp)
            if position:
                self.state.bankroll -= bet_size
                self.state.open_positions.append(position)
                slots -= 1
                log.info("  Posición de arbitraje abierta")
            self.state.analyzed_today.add(buy_m.condition_id)

        # ── ESTRATEGIA D: Smart Money Following (GRATIS — sin Claude) ─────────
        try:
            sm_copies = self.smart_money.run()
            if sm_copies > 0:
                slots = CONFIG["MAX_OPEN_BETS"] - len(self.state.open_positions)
        except Exception as e:
            log.error(f"Error en Smart Money: {e}")

        # ── ESTRATEGIA C: Edge con IA ─────────────────────────────────────────
        markets = all_markets_raw  # reusar los ya descargados
        markets = self.scanner.filter_markets(markets)            # Fase 1: filtros de código (gratis)
        markets = self.scanner.deduplicate_markets(markets)       # Eliminar variaciones del mismo evento

        # ── ESTRATEGIA 5: Contrarian Fade (gratis, sin Claude) ──────────
        try:
            self.contrarian.run(markets)
        except Exception as e:
            log.error(f"Error en Contrarian Fade: {e}")

        # Scoring inteligente: priorizar volumen MEDIO y categorías nicho
        def edge_score(m: Market) -> float:
            vol = m.volume
            if vol > 500_000:   vol_score = 0.2   # demasiado popular, bien preciado
            elif vol > 100_000: vol_score = 0.5
            elif vol > 25_000:  vol_score = 1.0   # sweet spot
            elif vol > 5_000:   vol_score = 0.8
            else:               vol_score = 0.3

            category_bonus = {
                "Politics": 1.3, "Science": 1.4, "Technology": 1.3,
                "Economics": 1.2, "Crypto": 0.6, "Sports": 0.5, "Pop Culture": 0.9,
            }
            cat_score = category_bonus.get(m.category, 1.0)

            best_dist = min(abs(o["price"] - 0.5) for o in m.outcomes) if m.outcomes else 0.5
            uncertainty_score = 1.0 + (0.5 - best_dist)  # más cercano a 0.50 = más potencial

            return vol_score * cat_score * uncertainty_score

        markets.sort(key=edge_score, reverse=True)
        log.info(f"Top categorías: {[m.category for m in markets[:5]]}")  # para calibrar bonuses
        markets = markets[:20]                                    # cap: 20 al pre-filtro

        # Excluir mercados ya analizados HOY antes de pasar a Claude
        # Así el pre-filtro no selecciona mercados que ya descartamos (~$0.01 ahorrado por ciclo)
        if CONFIG["MAX_MARKETS_PER_RUN"] == 0:
            log.info("MAX_MARKETS_PER_RUN=0 — saltando análisis Claude (modo gratuito)")
            markets = []
        else:
            fresh_markets = [m for m in markets if m.condition_id not in self.state.analyzed_today]
            if not fresh_markets:
                log.info("Todos los mercados prometedores ya fueron analizados hoy — saltando Phase 2/3")
                markets = []
            else:
                if len(fresh_markets) < len(markets):
                    log.info(f"Pre-filtro: excluyendo {len(markets)-len(fresh_markets)} ya analizados hoy")
                markets = self.analyzer.pre_filter_markets(fresh_markets)  # Fase 2: Claude sin web search (~$0.01)

            markets = markets[:CONFIG["MAX_MARKETS_PER_RUN"]]     # Fase 3: análisis profundo con web search
 
        opps = []
        for i, m in enumerate(markets):
            if m.condition_id in self.state.analyzed_today:
                continue
            # No re-analizar mercados donde ya tenemos posición
            if any(p.market_condition_id == m.condition_id for p in self.state.open_positions):
                continue
            log.info(f"Analizando [{i+1}/{len(markets)}]: {m.question[:65]}...")
            opp = self.analyzer.analyze_market(m, self.state.bankroll)
            if opp:
                opps.append(opp)
                log.info(f"  💡 {opp.outcome_name} | Edge: +{opp.edge*100:.1f}% | "
                         f"IA: {opp.ai_probability:.0%} vs Mkt: {opp.market_price:.0%} | "
                         f"Apuesta: ${opp.bet_size_usd}")
            else:
                log.info("  → Sin edge")
            self.state.analyzed_today.add(m.condition_id)
            time.sleep(3)  # rate limit
 
        # Ordenar por edge × confianza
        conf_w = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        opps.sort(key=lambda o: o.edge * conf_w.get(o.confidence, 1), reverse=True)
 
        # ── FASE 4: Abrir posiciones ───────────────────────────────────────────
        opened = 0
        for opp in opps[:slots]:
            if self.state.bankroll < opp.bet_size_usd + 5:   # reservar $5 siempre
                log.warning("Bankroll insuficiente para esta apuesta. Saltando.")
                continue
            position = self.executor.buy(opp)
            if position:
                self.state.bankroll -= opp.bet_size_usd       # descontar lo apostado
                self.state.open_positions.append(position)
                opened += 1
 
        log.info(
            f"═══ FIN CICLO | Abiertos: {opened} | "
            f"Posiciones activas: {len(self.state.open_positions)} | "
            f"Bankroll: ${self.state.bankroll:.2f} "
            f"({'+'if self.state.total_pnl>=0 else ''}{self.state.total_pnl_pct:.1f}% total) ═══"
        )
 
    def _monitor_positions(self):
        """Evalúa todas las posiciones abiertas y cierra las que corresponda."""
        if not self.state.open_positions:
            return
 
        log.info(f"Monitoreando {len(self.state.open_positions)} posiciones abiertas...")
        still_open = []
 
        for pos in self.state.open_positions:
            decision = self.monitor.evaluate(pos)
 
            log.info(
                f"  [{pos.id}] '{pos.outcome}' @ {pos.entry_price:.3f} → "
                f"{decision.current_price:.3f} | "
                f"PnL: {decision.unrealized_pnl_pct:+.1f}% | "
                f"{'CERRAR: '+decision.reason if decision.should_close else 'Mantener'}"
            )
 
            if decision.should_close:
                received = self.executor.sell(pos, decision.current_price)
                pnl = self.bankroll_mgr.update(self.state, received, pos)
 
                pos.status = "CLOSED_PROFIT" if pnl >= 0 else "CLOSED_LOSS"
                self.state.closed_positions.append(pos)
 
                # Verificar stop diario
                if self.state.daily_pnl <= -CONFIG["MAX_DAILY_LOSS"]:
                    log.warning(f"⛔ Stop diario: ${self.state.daily_pnl:.2f}")
                    self.state.daily_loss_triggered = True
                    break
            else:
                still_open.append(pos)
 
        self.state.open_positions = still_open
 
    def _check_daily_reset(self):
        """Resetea contadores diarios a medianoche."""
        today = datetime.now().strftime("%Y-%m-%d")
        session_day = self.state.session_start[:10]
        if today != session_day:
            log.info("Nuevo día — reseteando contadores diarios")
            self.state.daily_pnl = 0
            self.state.daily_loss_triggered = False
            # Preservar condition_ids de posiciones aún abiertas para evitar
            # re-entrada en mercados donde ya tenemos exposición
            open_cids = {p.market_condition_id for p in self.state.open_positions}
            self.state.analyzed_today = open_cids
            if open_cids:
                log.info(f"Preservados {len(open_cids)} condition_ids de posiciones abiertas en analyzed_today")
            self.state.session_start = datetime.now().isoformat()
 
    def _print_banner(self):
        mode = "DRY RUN 🧪" if CONFIG["DRY_RUN"] else "REAL 🔴   "
        print(f"""
╔══════════════════════════════════════════════════════════════╗
║  POLYMARKET AI AGENT v2.0   [{mode}]              ║
║  Bankroll:    ${self.state.bankroll:<10.2f}  (inicial: ${self.state.initial_bankroll:.2f})    ║
║  Take Profit: +{CONFIG['TAKE_PROFIT_PCT']*100:.0f}%      Stop Loss:  -{CONFIG['STOP_LOSS_PCT']*100:.0f}%           ║
║  Min Edge:    {CONFIG['MIN_EDGE']*100:.0f}%        Max Posiciones: {CONFIG['MAX_OPEN_BETS']}              ║
║  PnL total:   ${self.state.total_pnl:+.2f}      Win Rate: {self.state.win_rate:.0f}%             ║
╚══════════════════════════════════════════════════════════════╝
""")
 
    def _print_summary(self):
        closed = self.state.closed_positions
        wins   = sum(1 for p in closed if p.status == "CLOSED_PROFIT")
        print(f"""
═══════════════════════════════════════
  RESUMEN FINAL
═══════════════════════════════════════
  Bankroll inicial:  ${self.state.initial_bankroll:.2f}
  Bankroll final:    ${self.state.bankroll:.2f}
  PnL total:         ${self.state.total_pnl:+.2f} ({self.state.total_pnl_pct:+.1f}%)
  Trades cerrados:   {len(closed)}
  Win rate:          {self.state.win_rate:.0f}% ({wins}/{len(closed)})
  Posiciones activas:{len(self.state.open_positions)}
═══════════════════════════════════════
""")
 
 
# ═══════════════════════════════════════════════════════════
#  KEEP-ALIVE SERVER (para UptimeRobot / Replit free tier)
# ═══════════════════════════════════════════════════════════
def run_keep_alive():
    """Servidor Flask minimalista para que UptimeRobot haga ping y Replit no duerma."""
    from flask import Flask
    import threading

    app = Flask(__name__)

    @app.route("/")
    @app.route("/ping")
    @app.route("/api/alive")
    def ping():
        return {"status": "alive"}, 200

    port = int(os.getenv("PORT", 8080))
    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    )
    thread.start()
    log.info(f"Keep-alive server corriendo en puerto {port}")


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not CONFIG["ANTHROPIC_API_KEY"]:
        print("ERROR: Falta ANTHROPIC_API_KEY en el .env")
        sys.exit(1)
    run_keep_alive()
    PolymarketAgent().run()
