"""
╔══════════════════════════════════════════════════════════════╗
║        POLYMARKET AI AGENT v2.0 — powered by Claude         ║
║        Abre · Monitorea · Cierra · Reinvierte ganancias      ║
╚══════════════════════════════════════════════════════════════╝

CICLO COMPLETO:
    1. Scanner     → Descarga mercados activos de Polymarket
    2. Filter      → Filtra por liquidez, volumen y tiempo
    3. Analyzer    → Claude + web search estima probabilidad real
    4. Edge Calc   → Detecta discrepancia precio mercado vs IA
    5. Opener      → Abre posición con Kelly Criterion
    6. Monitor     → Revisa posiciones abiertas cada ciclo
    7. Closer      → Cierra si: take profit / stop loss / mercado resuelto
    8. Compounder  → Reinvierte ganancias al bankroll dinámico

REQUISITOS:
    pip install anthropic py-clob-client python-dotenv requests

AUTENTICACIÓN:

  Modo A — Proxy Wallet (Magic.link / Gmail login) ← recomendado para cuentas sociales
  ─────────────────────────────────────────────────
  Polymarket crea un "proxy wallet" para usuarios de Magic.link.
  Necesitas dos cosas:

    1. POLYMARKET_PROXY_ADDRESS  → tu dirección de wallet en Polymarket
       (visible en polymarket.com → Profile → Wallet address, empieza con 0x)

    2. POLYMARKET_PRIVATE_KEY    → tu clave de firma
       Obtenerla: polymarket.com → Profile → Export Key  (o desde Magic.link dashboard)

  Las credenciales de API (api_key / secret / passphrase) se derivan automáticamente.
  No necesitas configurarlas manualmente.

  Modo B — Wallet directa (clave privada raw)
  ─────────────────────────────────────────────────
  Si tienes una wallet EOA (MetaMask, etc.) conectada directamente:

    POLYMARKET_PRIVATE_KEY       → clave privada de tu wallet
    POLYMARKET_API_KEY           → (opcional, se auto-deriva si no se provee)
    POLYMARKET_API_SECRET        → (opcional)
    POLYMARKET_API_PASSPHRASE    → (opcional)

  El agente detecta automáticamente qué modo usar según las variables presentes.
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv
from flask import Flask, jsonify

import anthropic
import requests

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, ApiCreds
    from py_clob_client.constants import POLYGON
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    logging.warning("py-clob-client no disponible. Corriendo en modo DRY RUN.")

load_dotenv()

# ═══════════════════════════════════════════════════════════
#   CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════
CONFIG = {
    "ANTHROPIC_API_KEY":   os.getenv("ANTHROPIC_API_KEY", ""),

    # ── Auth: Modo A — Proxy Wallet (Magic.link / Gmail) ────
    # Provee ambas → el agente usa autenticación proxy wallet
    "PROXY_ADDRESS":       os.getenv("POLYMARKET_PROXY_ADDRESS", ""),   # tu wallet address en Polymarket
    "PRIVATE_KEY":         os.getenv("POLYMARKET_PRIVATE_KEY", ""),     # clave de firma (Export Key)

    # ── Auth: Modo B — API credentials manuales (opcional) ──
    # Si no se proveen, se auto-derivan de PRIVATE_KEY + PROXY_ADDRESS
    "API_KEY":             os.getenv("POLYMARKET_API_KEY", ""),
    "API_SECRET":          os.getenv("POLYMARKET_API_SECRET", ""),
    "API_PASSPHRASE":      os.getenv("POLYMARKET_API_PASSPHRASE", ""),

    # ── Bankroll ────────────────────────────────────────────
    "BANKROLL":            float(os.getenv("BANKROLL", "100")),
    "MIN_BET_USD":         float(os.getenv("MIN_BET_USD", "2.0")),
    "MAX_BET_USD":         float(os.getenv("MAX_BET_USD", "15.0")),
    "MAX_BET_PCT":         float(os.getenv("MAX_BET_PCT", "0.05")),

    # ── Edge y señales ──────────────────────────────────────
    "MIN_EDGE":            float(os.getenv("MIN_EDGE", "0.06")),

    # ── Take Profit / Stop Loss ─────────────────────────────
    "TAKE_PROFIT_PCT":     float(os.getenv("TAKE_PROFIT_PCT", "0.30")),
    "STOP_LOSS_PCT":       float(os.getenv("STOP_LOSS_PCT", "0.40")),
    "CLOSE_DAYS_LEFT":     int(os.getenv("CLOSE_DAYS_LEFT", "3")),
    "CLOSE_IF_EDGE_GONE":  os.getenv("CLOSE_IF_EDGE_GONE", "true").lower() == "true",

    # ── Riesgo global ───────────────────────────────────────
    "MAX_OPEN_BETS":       int(os.getenv("MAX_OPEN_BETS", "8")),
    "MAX_DAILY_LOSS":      float(os.getenv("MAX_DAILY_LOSS", "20")),
    "MIN_LIQUIDITY":       float(os.getenv("MIN_LIQUIDITY", "500")),
    "MIN_VOLUME":          float(os.getenv("MIN_VOLUME", "1000")),

    # ── Operación ───────────────────────────────────────────
    "SCAN_INTERVAL_MIN":   int(os.getenv("SCAN_INTERVAL_MIN", "30")),
    "MAX_MARKETS_PER_RUN": int(os.getenv("MAX_MARKETS_PER_RUN", "20")),
    "DRY_RUN":             os.getenv("DRY_RUN", "true").lower() == "true",
    "STATE_FILE":          "agent_state.json",
    "LOG_FILE":            "polymarket_agent.log",
}

# ═══════════════════════════════════════════════════════════
#   LOGGING
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
#   DATA CLASSES
# ═══════════════════════════════════════════════════════════
@dataclass
class Market:
    condition_id: str
    question: str
    category: str
    volume: float
    liquidity: float
    end_date: str
    outcomes: list
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
    id: str
    market_condition_id: str
    market_question: str
    outcome: str
    token_id: str
    size_usd: float
    shares: float
    entry_price: float
    ai_probability: float
    end_date: str
    opened_at: str
    status: str = "OPEN"


@dataclass
class CloseDecision:
    should_close: bool
    reason: str
    current_price: float = 0.0
    unrealized_pnl_usd: float = 0.0
    unrealized_pnl_pct: float = 0.0


@dataclass
class AgentState:
    bankroll: float
    initial_bankroll: float
    open_positions: list = field(default_factory=list)
    closed_positions: list = field(default_factory=list)
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
#   POLYMARKET API
# ═══════════════════════════════════════════════════════════
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


class PolymarketScanner:

    def get_active_markets(self, limit=300) -> list:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true", "closed": "false",
                    "limit": limit, "order": "volume24hr", "ascending": "false"
                },
                timeout=15
            )
            resp.raise_for_status()
            markets = []
            for m in resp.json():
                try:
                    outcomes = self._parse_outcomes(m)
                    if not outcomes:
                        continue
                    markets.append(Market(
                        condition_id=m.get("conditionId", m.get("id", "")),
                        question=m.get("question", ""),
                        category=m.get("category", "Other"),
                        volume=float(m.get("volume", 0)),
                        liquidity=float(m.get("liquidity", 0)),
                        end_date=m.get("endDate", ""),
                        outcomes=outcomes,
                        url=f"https://polymarket.com/event/{m.get('slug', '')}",
                    ))
                except Exception:
                    continue
            log.info(f"Descargados {len(markets)} mercados")
            return markets
        except Exception as e:
            log.error(f"Error descargando mercados: {e}")
            return []

    def _parse_outcomes(self, m: dict) -> list:
        outcomes = []
        for t in m.get("tokens", []):
            price = float(t.get("price", 0))
            if 0.01 <= price <= 0.99:
                outcomes.append({
                    "name": t.get("outcome", ""),
                    "token_id": t.get("tokenId", ""),
                    "price": price,
                })
        return outcomes

    def filter_markets(self, markets: list) -> list:
        filtered = []
        now = datetime.utcnow()
        for m in markets:
            if m.liquidity < CONFIG["MIN_LIQUIDITY"]:
                continue
            if m.volume < CONFIG["MIN_VOLUME"]:
                continue
            try:
                end = datetime.fromisoformat(m.end_date.replace("Z", ""))
                days_left = (end - now).days
                if days_left < 2 or days_left > 90:
                    continue
            except Exception:
                continue
            filtered.append(m)
        log.info(f"Mercados tras filtro: {len(filtered)}")
        return filtered

    def get_token_price(self, token_id: str) -> Optional[float]:
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

    def get_market_by_condition(self, condition_id: str) -> Optional[dict]:
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
#   CLAUDE ANALYZER
# ═══════════════════════════════════════════════════════════
class ClaudeAnalyzer:

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=CONFIG["ANTHROPIC_API_KEY"])

    def analyze_market(self, market: Market, bankroll: float) -> Optional[Opportunity]:
        outcomes_str = "\n".join(
            f"    - {o['name']}: precio = {o['price']:.3f} ({o['price']*100:.1f}%)"
            for o in market.outcomes
        )
        prompt = f"""Eres un analista experto en mercados de predicción. Detecta si hay discrepancia entre el precio del mercado y la probabilidad real.

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
    "has_edge": true,
    "best_outcome": "nombre del outcome",
    "best_outcome_token_id": "token_id",
    "market_price": 0.55,
    "ai_probability": 0.70,
    "edge": 0.15,
    "confidence": "HIGH/MEDIUM/LOW",
    "reasoning": "explicación detallada del edge"
}}

Si el edge es menor a 5% o no tienes info suficiente: has_edge: false, edge: 0."""

        try:
            response = self.client.messages.create(
                model="claude-opus-4-5",
                max_tokens=800,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            text = "".join(b.text for b in response.content if hasattr(b, "text") and b.type == "text")
            result = self._parse_json(text)
            if not result or not result.get("has_edge"):
                return None

            edge = float(result.get("edge", 0))
            if edge < CONFIG["MIN_EDGE"]:
                return None

            best_name = result.get("best_outcome", "")
            best_token = result.get("best_outcome_token_id", "")
            outcome = next(
                (o for o in market.outcomes if o["name"] == best_name or o["token_id"] == best_token),
                market.outcomes[0]
            )
            ai_prob = float(result.get("ai_probability", 0.5))
            mkt_price = float(result.get("market_price", outcome["price"]))
            confidence = result.get("confidence", "LOW")
            kelly = self._kelly(ai_prob, mkt_price) * 0.25
            bet = self._size_bet(kelly, confidence, bankroll)

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
        pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
        prompt = f"""Tienes una posición abierta en Polymarket. Decide si conviene CERRAR ahora.

POSICIÓN:
Mercado: "{position.market_question}"
Outcome apostado: {position.outcome}
Precio entrada: {position.entry_price:.3f}
Precio actual:  {current_price:.3f}
PnL actual:     {pnl_pct:+.1f}%
Probabilidad IA original: {position.ai_probability:.2f}
Fecha resolución: {position.end_date[:10]}

REGLA: Cierra si el mercado ya reflejó la información que te dio ventaja, o si hay nueva info negativa.

Busca noticias recientes sobre este mercado y decide.

RESPONDE SOLO EN JSON:
{{
    "should_close": true,
    "reason": "explicación de 1-2 oraciones",
    "updated_probability": 0.75
}}"""

        try:
            response = self.client.messages.create(
                model="claude-opus-4-5",
                max_tokens=300,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            text = "".join(b.text for b in response.content if hasattr(b, "text") and b.type == "text")
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
        if price <= 0 or price >= 1:
            return 0
        b = (1.0 / price) - 1
        if b <= 0:
            return 0
        return max(0, (prob * b - (1 - prob)) / b)

    def _size_bet(self, kelly: float, confidence: str, bankroll: float) -> float:
        mult = {"HIGH": 1.0, "MEDIUM": 0.7, "LOW": 0.4}.get(confidence, 0.4)
        max_by_pct = bankroll * CONFIG["MAX_BET_PCT"]
        raw = bankroll * kelly * mult
        return round(min(max(raw, CONFIG["MIN_BET_USD"]), CONFIG["MAX_BET_USD"], max_by_pct), 2)


# ═══════════════════════════════════════════════════════════
#   POSITION MONITOR
# ═══════════════════════════════════════════════════════════
class PositionMonitor:

    def __init__(self, scanner: PolymarketScanner, analyzer: ClaudeAnalyzer):
        self.scanner = scanner
        self.analyzer = analyzer

    def evaluate(self, position: Position) -> CloseDecision:
        current_price = self.scanner.get_token_price(position.token_id)
        if current_price is None:
            current_price = position.entry_price

        pnl_pct = (current_price - position.entry_price) / position.entry_price * 100
        pnl_usd = (current_price - position.entry_price) * position.shares

        if current_price >= 0.95:
            return CloseDecision(True, f"Mercado resuelto/casi resuelto a favor (precio={current_price:.3f})",
                                 current_price, pnl_usd, pnl_pct)

        if current_price <= 0.05:
            return CloseDecision(True, f"Mercado casi resuelto en contra (precio={current_price:.3f})",
                                 current_price, pnl_usd, pnl_pct)

        if pnl_pct <= -(CONFIG["STOP_LOSS_PCT"] * 100):
            return CloseDecision(True, f"Stop loss: {pnl_pct:.1f}% pérdida",
                                 current_price, pnl_usd, pnl_pct)

        if pnl_pct >= (CONFIG["TAKE_PROFIT_PCT"] * 100):
            return CloseDecision(True, f"Take profit: +{pnl_pct:.1f}% ganancia",
                                 current_price, pnl_usd, pnl_pct)

        try:
            end = datetime.fromisoformat(position.end_date.replace("Z", ""))
            days_left = (end - datetime.utcnow()).days
            if days_left <= CONFIG["CLOSE_DAYS_LEFT"] and pnl_pct > 0:
                return CloseDecision(True, f"Solo {days_left} días restantes, cerrando con ganancia",
                                     current_price, pnl_usd, pnl_pct)
        except Exception:
            pass

        if CONFIG["CLOSE_IF_EDGE_GONE"] and pnl_pct > 10:
            decision = self.analyzer.should_close_position(position, current_price)
            return decision

        return CloseDecision(False, "Mantener posición", current_price, pnl_usd, pnl_pct)


# ═══════════════════════════════════════════════════════════
#   EJECUTOR DE ÓRDENES
# ═══════════════════════════════════════════════════════════
class OrderExecutor:
    """
    Gestiona la conexión al CLOB de Polymarket y la ejecución de órdenes.

    Detecta automáticamente el modo de autenticación:

    Modo A — Proxy Wallet (Magic.link / Gmail)
        Requerido: POLYMARKET_PROXY_ADDRESS + POLYMARKET_PRIVATE_KEY
        Las credenciales de API se derivan automáticamente de la clave privada.
        El 'funder' en el CLOB es el proxy wallet address de Polymarket.

    Modo B — EOA directo (MetaMask u otra wallet no-proxy)
        Requerido: POLYMARKET_PRIVATE_KEY
        Opcional:  POLYMARKET_API_KEY / SECRET / PASSPHRASE
                   (si no se dan, se auto-derivan igual)
    """

    def __init__(self):
        self.clob = None
        self.auth_mode = "DRY_RUN"

        if CONFIG["DRY_RUN"]:
            log.info("Modo DRY RUN — órdenes simuladas")
            return

        if not CLOB_AVAILABLE:
            log.warning("py-clob-client no disponible — forzando DRY RUN")
            return

        if not CONFIG["PRIVATE_KEY"]:
            log.warning("POLYMARKET_PRIVATE_KEY no configurada — forzando DRY RUN")
            return

        try:
            self._connect()
        except Exception as e:
            log.error(f"Error conectando al CLOB: {e}")
            log.warning("Cayendo a DRY RUN por error de conexión")

    def _connect(self):
        """Conecta al CLOB detectando modo proxy wallet o EOA directo."""
        proxy_address = CONFIG["PROXY_ADDRESS"]
        private_key   = CONFIG["PRIVATE_KEY"]
        has_manual_creds = all([CONFIG["API_KEY"], CONFIG["API_SECRET"], CONFIG["API_PASSPHRASE"]])

        if proxy_address:
            # ── Modo A: Proxy Wallet (Magic.link / Gmail) ──────────────────
            log.info(f"Auth: Proxy Wallet — funder={proxy_address[:10]}...")
            self.auth_mode = "PROXY_WALLET"

            # Inicializar cliente sin creds todavía — sólo con clave de firma y funder
            self.clob = ClobClient(
                host=CLOB_API,
                chain_id=POLYGON,
                key=private_key,
                funder=proxy_address,
            )

            if has_manual_creds:
                # Usar credenciales manuales si se proveyeron
                self.clob.set_api_creds(ApiCreds(
                    api_key=CONFIG["API_KEY"],
                    api_secret=CONFIG["API_SECRET"],
                    api_passphrase=CONFIG["API_PASSPHRASE"],
                ))
                log.info("Credenciales API: manuales (provistas por env vars)")
            else:
                # Auto-derivar credenciales de la clave privada + proxy address
                log.info("Derivando credenciales API automáticamente...")
                creds = self.clob.create_or_derive_api_creds()
                self.clob.set_api_creds(creds)
                log.info(f"Credenciales API derivadas correctamente (key={creds.api_key[:8]}...)")

        else:
            # ── Modo B: EOA directo (sin proxy wallet) ─────────────────────
            log.info("Auth: EOA directo (sin proxy wallet)")
            self.auth_mode = "EOA_DIRECT"

            self.clob = ClobClient(
                host=CLOB_API,
                chain_id=POLYGON,
                key=private_key,
            )

            if has_manual_creds:
                self.clob.set_api_creds(ApiCreds(
                    api_key=CONFIG["API_KEY"],
                    api_secret=CONFIG["API_SECRET"],
                    api_passphrase=CONFIG["API_PASSPHRASE"],
                ))
                log.info("Credenciales API: manuales")
            else:
                log.info("Derivando credenciales API automáticamente...")
                creds = self.clob.create_or_derive_api_creds()
                self.clob.set_api_creds(creds)
                log.info(f"Credenciales API derivadas correctamente (key={creds.api_key[:8]}...)")

        log.info(f"CLOB conectado — modo REAL [{self.auth_mode}]")

    def buy(self, opp: Opportunity) -> Optional[Position]:
        import uuid
        shares = opp.bet_size_usd / opp.market_price

        if CONFIG["DRY_RUN"] or self.clob is None:
            log.info(
                f"[DRY RUN] BUY '{opp.outcome_name}' @ {opp.market_price:.3f} "
                f"| ${opp.bet_size_usd} → {shares:.2f} shares "
                f"| Edge: +{opp.edge*100:.1f}% | Conf: {opp.confidence}"
            )
        else:
            try:
                order = OrderArgs(
                    token_id=opp.token_id,
                    price=round(opp.market_price + 0.001, 3),
                    size=opp.bet_size_usd,
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
            size_usd=opp.bet_size_usd,
            shares=shares,
            entry_price=opp.market_price,
            ai_probability=opp.ai_probability,
            end_date=opp.market.end_date,
            opened_at=datetime.now().isoformat(),
        )

    def sell(self, position: Position, current_price: float) -> float:
        received = position.shares * current_price
        pnl_usd = received - position.size_usd
        pnl_pct = pnl_usd / position.size_usd * 100

        if CONFIG["DRY_RUN"] or self.clob is None:
            log.info(
                f"[DRY RUN] SELL '{position.outcome}' @ {current_price:.3f} "
                f"| Recibido: ${received:.2f} | PnL: {'+' if pnl_usd >= 0 else ''}{pnl_usd:.2f} ({pnl_pct:+.1f}%)"
            )
            return received

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
            return position.size_usd


# ═══════════════════════════════════════════════════════════
#   BANKROLL MANAGER
# ═══════════════════════════════════════════════════════════
class BankrollManager:

    def update(self, state: AgentState, received: float, position: Position) -> float:
        pnl = received - position.size_usd
        old_bankroll = state.bankroll

        state.bankroll += received
        state.daily_pnl += pnl
        state.total_invested += position.size_usd
        state.total_returned += received

        log.info(
            f"BANKROLL: ${old_bankroll:.2f} → ${state.bankroll:.2f} "
            f"(PnL: {'+' if pnl >= 0 else ''}{pnl:.2f} | Total: {state.total_pnl_pct:+.1f}%)"
        )
        return pnl


# ═══════════════════════════════════════════════════════════
#   ESTADO PERSISTENTE
# ═══════════════════════════════════════════════════════════
class StatePersistence:

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
            "open_positions": [vars(p) for p in state.open_positions],
            "closed_positions": [vars(p) for p in state.closed_positions[-100:]],
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
            state.open_positions = [Position(**p) for p in data.get("open_positions", [])]
            state.closed_positions = [Position(**p) for p in data.get("closed_positions", [])]
            log.info(f"Estado cargado: bankroll=${state.bankroll:.2f} | {len(state.open_positions)} posiciones abiertas")
            return state
        except Exception as e:
            log.error(f"Error cargando estado: {e}")
            return None


# ═══════════════════════════════════════════════════════════
#   AGENTE PRINCIPAL
# ═══════════════════════════════════════════════════════════
class PolymarketAgent:

    def __init__(self):
        self.scanner = PolymarketScanner()
        self.analyzer = ClaudeAnalyzer()
        self.monitor = PositionMonitor(self.scanner, self.analyzer)
        self.executor = OrderExecutor()
        self.bankroll_mgr = BankrollManager()

        saved = StatePersistence.load()
        if saved:
            self.state = saved
            log.info("Estado previo restaurado")
        else:
            self.state = AgentState(
                bankroll=CONFIG["BANKROLL"],
                initial_bankroll=CONFIG["BANKROLL"],
            )
            log.info(f"Nuevo agente iniciado con ${CONFIG['BANKROLL']} USDC")

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

        self._check_daily_reset()

        if self.state.daily_loss_triggered:
            log.warning("Stop diario activo. Saltando ciclo.")
            return

        self._monitor_positions()

        slots = CONFIG["MAX_OPEN_BETS"] - len(self.state.open_positions)
        if slots <= 0:
            log.info("Posiciones al máximo. Solo monitoreando.")
            return

        markets = self.scanner.get_active_markets()
        markets = self.scanner.filter_markets(markets)
        markets.sort(key=lambda m: m.volume, reverse=True)
        markets = markets[:CONFIG["MAX_MARKETS_PER_RUN"]]

        opps = []
        for i, m in enumerate(markets):
            if m.condition_id in self.state.analyzed_today:
                continue
            if any(p.market_condition_id == m.condition_id for p in self.state.open_positions):
                continue
            log.info(f"Analizando [{i+1}/{len(markets)}]: {m.question[:65]}...")
            opp = self.analyzer.analyze_market(m, self.state.bankroll)
            if opp:
                opps.append(opp)
                log.info(
                    f"  ✓ {opp.outcome_name} | Edge: +{opp.edge*100:.1f}% | "
                    f"IA: {opp.ai_probability:.0%} vs Mkt: {opp.market_price:.0%} | "
                    f"Apuesta: ${opp.bet_size_usd}"
                )
            else:
                log.info("  → Sin edge")
            self.state.analyzed_today.add(m.condition_id)
            time.sleep(3)

        conf_w = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        opps.sort(key=lambda o: o.edge * conf_w.get(o.confidence, 1), reverse=True)

        opened = 0
        for opp in opps[:slots]:
            if self.state.bankroll < opp.bet_size_usd + 5:
                log.warning("Bankroll insuficiente para esta apuesta. Saltando.")
                continue
            position = self.executor.buy(opp)
            if position:
                self.state.bankroll -= opp.bet_size_usd
                self.state.open_positions.append(position)
                opened += 1

        log.info(
            f"═══ FIN CICLO | Abiertos: {opened} | "
            f"Posiciones activas: {len(self.state.open_positions)} | "
            f"Bankroll: ${self.state.bankroll:.2f} "
            f"({'+'if self.state.total_pnl>=0 else ''}{self.state.total_pnl_pct:.1f}% total)"
        )

    def _monitor_positions(self):
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

                if self.state.daily_pnl <= -CONFIG["MAX_DAILY_LOSS"]:
                    log.warning(f"Stop diario: ${self.state.daily_pnl:.2f}")
                    self.state.daily_loss_triggered = True
                    break
            else:
                still_open.append(pos)

        self.state.open_positions = still_open

    def _check_daily_reset(self):
        today = datetime.now().strftime("%Y-%m-%d")
        session_day = self.state.session_start[:10]
        if today != session_day:
            log.info("Nuevo día — reseteando contadores diarios")
            self.state.daily_pnl = 0
            self.state.daily_loss_triggered = False
            self.state.analyzed_today = set()
            self.state.session_start = datetime.now().isoformat()

    def _print_banner(self):
        if CONFIG["DRY_RUN"]:
            mode_label = "DRY RUN"
        elif CONFIG["PROXY_ADDRESS"]:
            mode_label = "REAL · PROXY"
        else:
            mode_label = "REAL · EOA"
        print(f"""
╔══════════════════════════════════════════════════════════════╗
║    POLYMARKET AI AGENT v2.0   [{mode_label:^12}]           ║
║    Bankroll:    ${self.state.bankroll:<10.2f}  (inicial: ${self.state.initial_bankroll:.2f})
║    Take Profit: +{CONFIG['TAKE_PROFIT_PCT']*100:.0f}%          Stop Loss: -{CONFIG['STOP_LOSS_PCT']*100:.0f}%
║    Min Edge:    {CONFIG['MIN_EDGE']*100:.0f}%             Max Posiciones: {CONFIG['MAX_OPEN_BETS']}
║    PnL total:   ${self.state.total_pnl:+.2f}       Win Rate: {self.state.win_rate:.0f}%
╚══════════════════════════════════════════════════════════════╝
""")

    def _print_summary(self):
        closed = self.state.closed_positions
        wins = sum(1 for p in closed if p.status == "CLOSED_PROFIT")
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
#   KEEP-ALIVE SERVER (para UptimeRobot / pings externos)
# ═══════════════════════════════════════════════════════════
_flask_app = Flask(__name__)


@_flask_app.route("/alive")
@_flask_app.route("/")
def _alive():
    return jsonify({"status": "alive"}), 200


def start_keep_alive_server(port: int = 8000):
    """Inicia el servidor Flask keep-alive en un hilo daemon."""
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.ERROR)  # silenciar logs de Flask

    thread = threading.Thread(
        target=lambda: _flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    log.info(f"Flask keep-alive server en puerto {port} → GET /alive devuelve 200")
    return thread


# ═══════════════════════════════════════════════════════════
#   ENTRY POINT
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not CONFIG["ANTHROPIC_API_KEY"]:
        print("ERROR: Falta ANTHROPIC_API_KEY en el .env o variables de entorno")
        sys.exit(1)
    start_keep_alive_server(port=8000)
    PolymarketAgent().run()
