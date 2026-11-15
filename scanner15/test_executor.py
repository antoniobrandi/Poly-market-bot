"""
Pruebas del ejecutor sin red y sin dinero: Gamma, CLOB y reloj son falsos.
    python -m pytest -q test_executor.py
"""
import csv
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import executor as E  # noqa: E402

T0 = 1_763_200_800          # inicio de una ventana de 15 min (múltiplo de 900)


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeGamma:
    def __init__(self):
        self.resolved = {}   # slug → "UP"/"DOWN"

    def market(self, slug):
        win = self.resolved.get(slug)
        return {
            "slug": slug, "outcomes": '["Up", "Down"]',
            "clobTokenIds": json.dumps([f"{slug}:UP", f"{slug}:DOWN"]),
            "acceptingOrders": win is None, "closed": win is not None,
            "outcomePrices": json.dumps(["1", "0"] if win == "UP" else ["0", "1"]) if win else '["0.5","0.5"]',
            "orderMinSize": 0, "orderPriceMinTickSize": 0.01, "negRisk": False,
        }


class FakeClob:
    """Simula CLOB: libro con un ask fijo; órdenes que se llenan según `fill`."""

    def __init__(self, best_ask=0.50, min_size=0, fill="all_now"):
        self.best_ask, self.min_size, self.fill = best_ask, min_size, fill
        self.created, self.posted, self.cancelled, self.market_orders = [], [], [], 0
        self._polls = 0

    def get_order_book(self, token):
        return {"asks": [{"price": str(self.best_ask), "size": "100"}, {"price": "0.99", "size": "5"}],
                "bids": [], "tick_size": "0.01", "min_order_size": str(self.min_size), "neg_risk": False}

    def create_order(self, args, options):
        self.created.append((args, options))
        return {"args": args}

    def create_market_order(self, *a, **k):
        self.market_orders += 1
        raise AssertionError("¡nunca debe usar órdenes a mercado!")

    def post_order(self, order, order_type):
        self.posted.append(order_type)
        a = order["args"]
        if self.fill == "all_now":
            return {"success": True, "orderID": "0xabc", "status": "matched",
                    "makingAmount": str(round(a.size * a.price, 4)), "takingAmount": str(a.size)}
        return {"success": True, "orderID": "0xabc", "status": "live"}

    def get_order(self, oid):
        self._polls += 1
        a = self.created[-1][0]
        if self.fill == "partial":
            return {"status": "LIVE", "size_matched": str(round(a.size / 2, 2))}
        return {"status": "LIVE", "size_matched": "0"}

    def cancel_order(self, payload):
        self.cancelled.append(payload.orderID)
        return {"canceled": [payload.orderID]}


def make(tmp_path, live=False, t=T0 + 60, **clob_kw):
    clk = Clock(t)
    g, c = FakeGamma(), FakeClob(**clob_kw)
    ex = E.Executor(live=live, clob=c, gamma=g, base_dir=str(tmp_path),
                    now=clk.now, sleep=clk.sleep, fill_timeout=10, poll_s=1)
    return ex, clk, g, c


def sig(clk, coin="btc", side="UP", price=0.50, edge=0.05, ws=T0):
    return E.Signal(coin, side, price, edge, window_start=ws, ts=clk.now())


def rows(ex):
    with open(ex.csv_path) as f:
        return list(csv.DictReader(f))


# ── 1. DRY_RUN por default ──────────────────────────────────
def test_dry_run_is_default(tmp_path):
    ex = E.Executor(clob=FakeClob(), gamma=FakeGamma(), base_dir=str(tmp_path))
    assert ex.mode == "DRY_RUN" and not ex.live
    assert ex.csv_path.endswith("ordenes_dry.csv")


def test_dry_run_never_creates_or_posts_orders(tmp_path):
    ex, clk, g, c = make(tmp_path)
    [rec] = ex.handle_signals([sig(clk)])
    assert rec["estado"] == "LLENADA" and rec["modo"] == "DRY_RUN"
    assert c.created == [] and c.posted == [] and c.cancelled == []
    assert not os.path.exists(os.path.join(tmp_path, "ordenes.csv"))   # el real queda limpio


def test_cli_without_live_uses_public_client(tmp_path, monkeypatch):
    """main() sin --live jamás pide el cliente con llave."""
    called = {}
    monkeypatch.setattr(E, "HERE", str(tmp_path))
    monkeypatch.setattr(E.Executor, "place", lambda self, s: called.setdefault("mode", self.mode))
    E.main(["--signal", "btc", "UP", "0.50", "0.05"])
    assert called.get("mode") in (None, "DRY_RUN")


# ── 2. $1 y una orden por ventana (la de mayor edge) ────────
def test_one_order_per_window_picks_best_edge(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    recs = ex.handle_signals([sig(clk, "btc", edge=0.04), sig(clk, "eth", edge=0.09),
                              sig(clk, "sol", "DOWN", edge=0.06)])
    assert len(recs) == 1 and recs[0]["moneda"] == "ETH"
    assert len(c.posted) == 1
    # otro ciclo en la misma ventana: no opera
    assert ex.handle_signals([sig(clk, "xrp", edge=0.20)]) == []
    assert len(c.posted) == 1
    # siguiente ventana: sí
    clk.t = T0 + 900 + 30
    assert len(ex.handle_signals([sig(clk, "xrp", ws=T0 + 900)])) == 1


def test_size_is_one_dollar(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    ex.handle_signals([sig(clk, price=0.53)])
    args, _ = c.created[0]
    assert args.side == "BUY"
    assert 1.0 <= args.size * args.price < 1.01        # ≈ $1, nunca menos


def test_cancelled_unfilled_order_still_uses_window(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="never")
    ex.handle_signals([sig(clk)])
    assert ex.handle_signals([sig(clk, "eth")]) == []
    assert len(c.posted) == 1


# ── 3. Límite al ask; cancelar a los 10 s ───────────────────
def test_limit_order_at_signal_ask_gtc_not_market(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    ex.handle_signals([sig(clk, price=0.537)])
    args, opts = c.created[0]
    assert args.price == pytest.approx(0.53)          # redondeo hacia abajo al tick
    assert c.posted == ["GTC"] and c.market_orders == 0
    assert opts.tick_size == "0.01"


def test_unfilled_cancels_after_10s(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="never")
    t_start = clk.t
    [rec] = ex.handle_signals([sig(clk)])
    assert c.cancelled == ["0xabc"]
    assert 10 <= clk.t - t_start <= 12
    assert rec["estado"] == "CANCELADA" and rec["resultado"] == "—"
    assert float(rec["pnl_usd"]) == 0


def test_partial_fill_is_recorded(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="partial")
    [rec] = ex.handle_signals([sig(clk)])
    assert c.cancelled == ["0xabc"]
    assert rec["estado"] == "PARCIAL"
    assert 0 < rec["acciones_llenadas"] < rec["acciones_pedidas"]
    assert rec["resultado"] == "PENDIENTE"


def test_dry_run_cancels_when_ask_above_limit(tmp_path):
    ex, clk, g, c = make(tmp_path, best_ask=0.55)
    [rec] = ex.handle_signals([sig(clk, price=0.50)])
    assert rec["estado"] == "CANCELADA"
    assert clk.t - (T0 + 60) >= 10


def test_stop_during_wait_cancels_immediately(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="never")
    orig = c.get_order

    def get_order_then_stop(oid):
        open(ex.stop_path, "w").close()
        return orig(oid)
    c.get_order = get_order_then_stop
    t_start = clk.t
    ex.handle_signals([sig(clk)])
    assert c.cancelled == ["0xabc"] and clk.t - t_start < 3


# ── 4. Pérdida diaria $3 ────────────────────────────────────
def _lose_window(ex, clk, g, k):
    ws = T0 + 900 * k
    clk.t = ws + 60
    recs = ex.handle_signals([sig(clk, ws=ws)])
    if recs:
        g.resolved[recs[0]["slug"]] = "DOWN"            # apostamos UP → pierde
    clk.t = ws + 900 + 60
    ex.settle_pending()
    return recs


def test_daily_loss_limit_then_next_day(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    placed = 0
    for k in range(8):
        placed += len(_lose_window(ex, clk, g, k))
    assert ex.realized_today() >= -E.MAX_DAILY_LOSS - 1e-9      # nunca pasa de -$3
    assert placed == 2                                           # 2 × ≈$1.03; una 3ª pasaría $3
    assert "hasta mañana" in ex.blocked_reason(new_risk=1.03)
    # Día siguiente: vuelve a operar
    clk.t = T0 + 86400 * 2
    ws = int(clk.t // 900 * 900)
    clk.t = ws + 60
    assert len(ex.handle_signals([sig(clk, ws=ws)])) == 1


def test_open_positions_count_as_risk(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    ex.state["orders"].append({"fecha": E.datetime.fromtimestamp(clk.t).strftime("%Y-%m-%d %H:%M:%S"),
                               "resultado": "PIERDE", "pnl_usd": -2.5, "costo_usd": 1, "comision_est_usd": 0})
    assert ex.handle_signals([sig(clk)]) == []        # -2.5 - 1 < -3 en el peor caso
    assert c.posted == []


# ── 5. Archivo STOP ─────────────────────────────────────────
def test_stop_file_blocks(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    open(ex.stop_path, "w").close()
    assert ex.handle_signals([sig(clk)]) == []
    assert c.posted == []
    os.remove(ex.stop_path)
    assert len(ex.handle_signals([sig(clk)])) == 1


# ── 6. ordenes.csv ──────────────────────────────────────────
def test_csv_has_signal_fill_result_pnl(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    [rec] = ex.handle_signals([sig(clk, price=0.50)])
    assert ex.csv_path.endswith("ordenes.csv")
    [r] = rows(ex)
    assert r["resultado"] == "PENDIENTE" and float(r["precio_senal"]) == 0.5
    assert float(r["precio_llenado"]) == pytest.approx(0.5)
    g.resolved[rec["slug"]] = "UP"
    clk.t = T0 + 900 + 60
    assert ex.settle_pending() == 1
    [r] = rows(ex)
    assert r["resultado"] == "GANA"
    sh = float(r["acciones_llenadas"])
    assert float(r["pago_usd"]) == pytest.approx(sh)
    exp = sh - float(r["costo_usd"]) - float(r["comision_est_usd"])
    assert float(r["pnl_usd"]) == pytest.approx(exp, abs=1e-3)
    assert float(r["comision_est_usd"]) == pytest.approx(sh * 0.07 * 0.25, abs=1e-3)


def test_state_survives_restart(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    ex.handle_signals([sig(clk)])
    ex2 = E.Executor(live=True, clob=c, gamma=g, base_dir=str(tmp_path), now=clk.now, sleep=clk.sleep)
    assert ex2.handle_signals([sig(clk, "eth")]) == []   # la ventana sigue usada


# ── 7. Filtros de seguridad ─────────────────────────────────
@pytest.mark.parametrize("kw,t", [
    (dict(price=0.715), T0 + 60),     # fuera de 30–70¢ (como la de ETH a 71.5¢)
    (dict(price=0.294), T0 + 60),     # (como la de XRP a 29.4¢)
    (dict(edge=0.02), T0 + 60),       # edge chico
    (dict(), T0 + 900 - 100),         # quedan < 2 min
])
def test_filters(tmp_path, kw, t):
    ex, clk, g, c = make(tmp_path, live=True, t=t)
    assert ex.handle_signals([sig(clk, **kw)]) == []
    assert c.posted == []


def test_stale_signal_rejected(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    s = sig(clk)
    clk.t += 45
    assert ex.handle_signals([s]) == []


def test_below_market_minimum_skips_without_using_window(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, min_size=5)
    assert ex.handle_signals([sig(clk)]) == []
    assert c.posted == [] and str(T0) not in ex.state["windows"]


# ── 8. Lectura de senales.csv ───────────────────────────────
def test_csv_follower_reads_new_rows(tmp_path):
    path = tmp_path / "senales.csv"
    path.write_text("hora,moneda,señal,ask,edge_neto,mercado\n"
                    "10:00:00,BTC,UP,51.0¢,3.5¢,btc-updown-15m-1763200800\n")
    fol = E.CsvFollower(str(path), {})
    assert fol.poll() == []                        # lo viejo no se re-ejecuta
    with open(path, "a") as f:
        f.write(f"{E.datetime.now():%H:%M:%S},ETH,DOWN,0.48,0.041,eth-updown-15m-1763201700\n")
        f.write("10:01:00,SOL,,0.5,0.01,sol-updown-15m-1763201700\n")      # sin señal
        f.write("10:01:00,XRP,UP,0.5")                                     # línea a medias
    got = fol.poll()
    assert len(got) == 1
    s = got[0]
    assert (s.coin, s.side, s.window_start) == ("eth", "DOWN", 1763201700)
    assert s.price == pytest.approx(0.48) and s.edge == pytest.approx(0.041)
    with open(path, "a") as f:
        f.write(",0.04,xrp-updown-15m-1763201700\n")
    [x] = fol.poll()
    assert x.coin == "xrp" and x.edge == pytest.approx(0.04)


def test_parsers():
    assert E.parse_num("52.3¢") == pytest.approx(0.523)
    assert E.parse_num("0.523") == pytest.approx(0.523)
    assert E.parse_num("3.1") == pytest.approx(0.031)
    assert E.parse_side("🟢 UP") == "UP" and E.parse_side("Baja") == "DOWN"
    assert E.parse_side("") is None and E.parse_side("—") is None
