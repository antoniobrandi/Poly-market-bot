"""
Pruebas del scanner sin red: Gamma, CLOB, Binance y Polymarket son falsos.
    python -m pytest -q test_scanner.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner as S  # noqa: E402

START = 1_794_731_400
TARGET = 64_000.0


# ── base Binance↔Chainlink ──────────────────────────────────
def test_measure_basis():
    assert S.measure_basis(TARGET * 1.00056, TARGET) == pytest.approx(0.00056)
    assert S.measure_basis(TARGET * 1.004, TARGET) is None        # > 30 pb: dato raro
    assert S.measure_basis(None, TARGET) is None


def test_basis_correction_removes_fake_up_bias():
    """Con Binance 5.6 pb arriba del oficial y el precio 'parado', el modelo v1
    veía ~56 % Up; corregido debe ver 50 %."""
    sigma = 0.001 / 60 ** 0.5                                     # 0.1 %/min
    raw = S.fair_prob_up(TARGET * 1.00056, TARGET, sigma, 890)
    fixed = S.fair_prob_up(TARGET * 1.00056 / 1.00056, TARGET, sigma, 890)
    assert raw > 0.55 and fixed == pytest.approx(0.5, abs=1e-6)


def test_twap_before_uses_1s_klines(monkeypatch):
    calls = []

    def fake_klines(sym, interval, limit, start_ms=None):
        calls.append(interval)
        return [[start_ms + i * 1000, 0, 0, 0, str(100 + i % 2)] for i in range(limit)]
    monkeypatch.setattr(S, "bn_klines", fake_klines)
    f = S.Feed("binance", "BTCUSDT")
    assert f.twap_before(START) == pytest.approx(100.5)
    assert calls == ["1s"]


# ── scan_coin con todo simulado ─────────────────────────────
class FakeFeed:
    def __init__(self, price, twap):
        self._p, self._twap, self.twap_calls = price, twap, 0

    def price(self):
        return self._p

    def candles(self):
        c1 = [TARGET * (1 + 0.0005 * ((-1) ** i)) for i in range(120)]
        return c1, [TARGET] * 60

    def depth(self):
        return [(TARGET - 1, 1.0)], [(TARGET + 1, 1.0)]

    def open_at(self, start):
        return TARGET

    def twap_before(self, start, lookback=60):
        self.twap_calls += 1
        return self._twap


@pytest.fixture
def market(monkeypatch):
    monkeypatch.setattr(S, "fetch_event", lambda coin, start: {
        "slug": f"{coin}-updown-15m-{start}", "closed": False, "up_token": "U", "down_token": "D",
        "best_bid": None, "best_ask": None, "fee": S.DEFAULT_FEE, "lookback": 60, "outcome_prices": {}})
    monkeypatch.setattr(S, "fetch_target", lambda *a, **k: (TARGET, "PM"))
    monkeypatch.setattr(S, "clob_best_ask", lambda tok: (None, 0.45 if tok == "U" else 0.56))
    monkeypatch.setattr(S.time, "time", lambda: START + 10)
    S.BASIS_LAST.clear()


def test_scan_coin_applies_basis_once(market):
    feed = FakeFeed(price=TARGET * 1.00056, twap=TARGET * 1.00056)   # Binance arriba, precio quieto
    cache = {}
    r, _ = S.scan_coin("btc", feed, "BTC", START, cache)
    assert r.basis == pytest.approx(0.00056)
    assert r.fair_up == pytest.approx(0.5, abs=0.01)                  # ya no ve Up "barato"
    fee = S.taker_fee_per_share(0.45, S.DEFAULT_FEE)
    assert r.edge_up == pytest.approx(r.fair_up - 0.45 - fee)         # edge sobre la prob. corregida
    S.scan_coin("btc", feed, "BTC", START, cache)
    assert feed.twap_calls == 1                                       # una medición por ventana


def test_v1_would_have_signaled_the_fake_edge(market, monkeypatch):
    monkeypatch.setattr(S, "MODEL_V", 1)
    feed = FakeFeed(price=TARGET * 1.00056, twap=TARGET * 1.00056)
    r, _ = S.scan_coin("btc", feed, "BTC", START, {})
    assert r.basis == 0 and r.fair_up > 0.55


def test_basis_falls_back_to_last_known(market):
    S.BASIS_LAST["btc"] = 0.0004
    feed = FakeFeed(price=TARGET, twap=None)                          # falla la medición
    r, _ = S.scan_coin("btc", feed, "BTC", START, {})
    assert r.basis == pytest.approx(0.0004)


# ── papel separado por versión ──────────────────────────────
def test_paper_stats_split_by_model_version(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "PAPER_JSON", str(tmp_path / "papel.json"))
    p = S.Paper([{"slug": "old", "coin": "btc", "side": "Up", "ask": 0.5, "fair": 0.6,
                  "shares": 2, "stake": 1, "result": "L", "pnl": -1.0}])   # sin "v" = v1
    p.add("new", "btc", "Up", 0.5, 0.55, S.DEFAULT_FEE, 890, basis=0.0005)
    p.trades[-1].update(result="W", pnl=0.93)
    assert p.stats(1)["n"] == 1 and p.stats(1)["pnl"] == -1.0
    assert p.stats(2)["n"] == 1 and p.trades[-1]["basis_pb"] == 5.0
    assert p.stats()["n"] == 2


def test_run_cycle_logs_signal_once(tmp_path, monkeypatch, market):
    monkeypatch.setattr(S, "PAPER_JSON", str(tmp_path / "papel.json"))
    monkeypatch.setattr(S, "SIGNALS_CSV", str(tmp_path / "senales.csv"))
    # precio claramente arriba del target en escala Chainlink → señal Up
    feeds = {"btc": FakeFeed(price=TARGET * 1.003, twap=TARGET)}
    monkeypatch.setattr(S, "clob_best_ask", lambda tok: (None, 0.55 if tok == "U" else 0.47))
    seen = []
    paper, cache = S.Paper(), {}
    S.run_cycle(["btc"], feeds, paper, cache, on_signal=lambda r, a: seen.append((r.coin, r.signal)))
    S.run_cycle(["btc"], feeds, paper, cache, on_signal=lambda r, a: seen.append((r.coin, r.signal)))
    assert seen == [("BTC", "Up")]                                    # una sola vez por mercado
    lines = open(tmp_path / "senales.csv").read().splitlines()
    assert lines[0].startswith("hora,slug,moneda,lado,precio") and len(lines) == 2


def test_recorder_snapshots_once_a_minute_and_resolves(tmp_path, monkeypatch, market):
    monkeypatch.setattr(S, "MARKET_CSV", str(tmp_path / "mercado.csv"))
    monkeypatch.setattr(S, "RESULTS_CSV", str(tmp_path / "resultados.csv"))
    monkeypatch.setattr(S, "PAPER_JSON", str(tmp_path / "papel.json"))
    monkeypatch.setattr(S, "SIGNALS_CSV", str(tmp_path / "senales.csv"))
    clock = {"t": START + 10}
    monkeypatch.setattr(S.time, "time", lambda: clock["t"])
    feeds = {"btc": FakeFeed(price=TARGET, twap=TARGET)}
    rec, paper, cache = S.Recorder(), S.Paper(), {}
    for dt in (0, 5, 30, 61):                                         # 2 fotos: t=0 y t=61
        clock["t"] = START + 10 + dt
        S.run_cycle(["btc"], feeds, paper, cache, recorder=rec)
    rows = open(tmp_path / "mercado.csv").read().splitlines()
    assert len(rows) == 3 and rows[0].startswith("hora,ts,slug,moneda")
    # la ventana termina y el mercado resuelve Down
    monkeypatch.setattr(S, "fetch_event", lambda coin, start: {"outcome_prices": {"Up": 0.0, "Down": 1.0}})
    clock["t"] = START + 900 + 120
    rec.resolve()
    res = open(tmp_path / "resultados.csv").read().splitlines()
    assert res == ["slug,moneda,inicio,ganador", f"btc-updown-15m-{START},BTC,{START},Down"]
    rec.resolve()                                                     # no duplica
    assert len(open(tmp_path / "resultados.csv").read().splitlines()) == 2


# ── página de estado ────────────────────────────────────────
def test_webstatus_requires_token_and_only_serves_listed_files(tmp_path, monkeypatch):
    import importlib
    import json
    import threading
    import urllib.error
    import urllib.request
    monkeypatch.setenv("SCANNER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_TOKEN", "secreto")
    import webstatus as W
    W = importlib.reload(W)
    (tmp_path / "papel.json").write_text(json.dumps(
        [{"v": 2, "fair": 0.55, "result": "W", "pnl": 0.9}, {"fair": 0.6, "result": "L", "pnl": -1}]))
    (tmp_path / ".env").write_text("POLYMARKET_PRIVATE_KEY=nunca")
    srv = W.ThreadingHTTPServer(("127.0.0.1", 0), W.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        try:
            with opener.open(base + path, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""
    assert get("/")[0] == 403 and get("/?t=mal")[0] == 403
    code, body = get("/?t=secreto")
    assert code == 200 and "modelo v2: 1 cerradas" in body and "modelo v1: 1 cerradas" in body
    assert get("/f/papel.json?t=secreto")[0] == 200
    assert get("/f/.env?t=secreto")[0] == 404
    assert get("/f/../.env?t=secreto")[0] == 404
    srv.shutdown()
