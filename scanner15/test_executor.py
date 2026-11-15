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
    """CLOB falso: libro con un ask fijo y compras FAK por monto.
    fill: all_now | partial | never | reject ; fill_after: intentos sin llenar antes de llenar."""

    def __init__(self, best_ask=0.50, fill="all_now", fill_after=0):
        self.best_ask, self.fill, self.fill_after = best_ask, fill, fill_after
        self.market_args, self.posted, self.limit_orders = [], [], 0

    def get_order_book(self, token):
        return {"asks": [{"price": str(self.best_ask), "size": "100"}, {"price": "0.99", "size": "5"}],
                "bids": [], "tick_size": "0.01", "min_order_size": "5", "neg_risk": False}

    def create_order(self, *a, **k):
        self.limit_orders += 1
        raise AssertionError("no debe dejar órdenes en el libro")

    def create_market_order(self, args, options):
        self.market_args.append((args, options))
        return {"args": args}

    def post_order(self, order, order_type):
        self.posted.append(order_type)
        a, n = order["args"], len(self.posted)
        if self.fill == "reject":
            return {"success": False, "errorMsg": "not enough balance / allowance"}
        if self.fill == "never" or n <= self.fill_after:
            return {"success": False, "errorMsg": "no orders found to match with FAK order"}
        usd = a.amount if self.fill == "all_now" else a.amount / 2
        return {"success": True, "orderID": f"0x{n}", "status": "matched",
                "makingAmount": str(usd), "takingAmount": str(round(usd / a.price, 4))}


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
    assert c.market_args == [] and c.posted == []
    assert not os.path.exists(os.path.join(tmp_path, "ordenes.csv"))   # el real queda limpio


def test_cli_without_live_uses_public_client(tmp_path, monkeypatch):
    """main() sin --live jamás pide el cliente con llave."""
    called = {}
    monkeypatch.setattr(E, "HERE", str(tmp_path))
    monkeypatch.setattr(E.Executor, "place", lambda self, s: called.setdefault("mode", self.mode))
    E.main(["--signal", "btc", "UP", "0.50", "0.05"])
    assert called.get("mode") in (None, "DRY_RUN")


# ── 2. $1 por señal, como el papel: una orden por mercado ───
def test_every_signal_is_an_order_like_paper(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    recs = ex.handle_signals([sig(clk, "btc", edge=0.04), sig(clk, "eth", edge=0.09),
                              sig(clk, "sol", "DOWN", edge=0.06)])
    assert [r["moneda"] for r in recs] == ["BTC", "ETH", "SOL"]
    assert len(c.posted) == 3
    # misma moneda y ventana otra vez: no repite (como paper.has(slug))
    assert ex.handle_signals([sig(clk, "btc", edge=0.20)]) == []
    assert len(c.posted) == 3
    # otra moneda en la misma ventana: sí
    assert len(ex.handle_signals([sig(clk, "xrp")])) == 1
    # siguiente ventana, misma moneda: sí (las anteriores ya se resolvieron)
    for o in ex.state["orders"]:
        g.resolved[o["slug"]] = "UP"
    clk.t = T0 + 900 + 30
    ex.settle_pending()
    assert len(ex.handle_signals([sig(clk, "btc", ws=T0 + 900)])) == 1


def test_size_is_one_dollar(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    [rec] = ex.handle_signals([sig(clk, price=0.53)])
    args, _ = c.market_args[0]
    assert args.side == "BUY" and args.amount == 1.0       # $1 exacto, sin importar acciones
    assert float(rec["costo_usd"]) == pytest.approx(1.0)
    assert float(rec["acciones_llenadas"]) == pytest.approx(1 / 0.53, abs=1e-3)


def test_unfilled_still_uses_market(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="never")
    ex.handle_signals([sig(clk)])
    n = len(c.posted)
    assert ex.handle_signals([sig(clk)]) == []          # mismo mercado: no vuelve a entrar
    assert len(c.posted) == n


# ── 3. Tope = ask de la señal; reintenta hasta 10 s ─────────
def test_price_cap_is_signal_ask_fak_never_resting(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    ex.handle_signals([sig(clk, price=0.537)])
    args, opts = c.market_args[0]
    assert args.price == pytest.approx(0.53)          # tope redondeado hacia abajo al tick
    assert args.order_type == "FAK" and c.posted == ["FAK"]
    assert c.limit_orders == 0 and opts.tick_size == "0.01"


def test_min_5_shares_does_not_block_one_dollar(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)          # el libro dice min_order_size 5
    [rec] = ex.handle_signals([sig(clk, price=0.50)])
    assert rec["estado"] == "LLENADA" and float(rec["acciones_llenadas"]) == pytest.approx(2.0)


def test_unfilled_retries_10s_then_gives_up(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="never")
    t_start = clk.t
    [rec] = ex.handle_signals([sig(clk)])
    assert 10 <= clk.t - t_start <= 12 and 10 <= len(c.posted) <= 12
    assert rec["estado"] == "SIN_LLENAR" and rec["resultado"] == "—"
    assert float(rec["pnl_usd"]) == 0


def test_fills_on_a_retry(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill_after=3)
    [rec] = ex.handle_signals([sig(clk)])
    assert len(c.posted) == 4 and rec["estado"] == "LLENADA"


def test_partial_fill_is_recorded(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="partial")
    [rec] = ex.handle_signals([sig(clk)])
    assert rec["estado"] == "PARCIAL" and float(rec["costo_usd"]) == pytest.approx(0.5)
    assert rec["resultado"] == "PENDIENTE" and len(c.posted) == 1


def test_rejection_is_not_retried(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="reject")
    [rec] = ex.handle_signals([sig(clk)])
    assert rec["estado"] == "RECHAZADA" and len(c.posted) == 1


def test_dry_run_no_fill_when_ask_above_cap(tmp_path):
    ex, clk, g, c = make(tmp_path, best_ask=0.55)
    [rec] = ex.handle_signals([sig(clk, price=0.50)])
    assert rec["estado"] == "SIN_LLENAR"
    assert clk.t - (T0 + 60) >= 10


def test_stop_during_retries_stops(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True, fill="never")
    orig = c.post_order

    def post_then_stop(order, ot):
        open(ex.stop_path, "w").close()
        return orig(order, ot)
    c.post_order = post_then_stop
    t_start = clk.t
    [rec] = ex.handle_signals([sig(clk)])
    assert len(c.posted) == 1 and clk.t - t_start < 2 and rec["estado"] == "SIN_LLENAR"


# ── 4. Pérdida diaria $5 ────────────────────────────────────
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
    assert E.MAX_DAILY_LOSS == 5.0
    assert placed == 5                     # 4 pérdidas = −4.14 sigue; la 5ª pasa de −$5 y para
    assert ex.realized_today() > -E.MAX_DAILY_LOSS - 1.1
    assert "límite diario alcanzado" in ex.blocked_reason()
    # Día siguiente: vuelve a operar
    clk.t = T0 + 86400 * 2
    ws = int(clk.t // 900 * 900)
    clk.t = ws + 60
    assert len(ex.handle_signals([sig(clk, ws=ws)])) == 1


def test_open_positions_do_not_block(tmp_path):
    ex, clk, g, c = make(tmp_path, live=True)
    ex.state["orders"].append({"fecha": E.datetime.fromtimestamp(clk.t).strftime("%Y-%m-%d %H:%M:%S"),
                               "resultado": "PIERDE", "pnl_usd": -4.5, "costo_usd": 1, "comision_est_usd": 0})
    assert len(ex.handle_signals([sig(clk)])) == 1    # −4.5 cerrado aún no llega a −5


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
    assert ex2.handle_signals([sig(clk)]) == []          # el mercado sigue usado


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


def test_real_scanner_csv_uses_side_ask_not_spot(tmp_path):
    """Formato real de scanner.py: 'precio' es el spot; el precio va en up_ask/down_ask."""
    path = tmp_path / "senales.csv"
    path.write_text("hora,slug,moneda,lado,precio,target,seg_rest,prob_justa_up,up_ask,down_ask,edge,tendencia,libro\n")
    fol = E.CsvFollower(str(path), {})
    assert fol.poll() == []
    with open(path, "a") as f:
        f.write("2026-11-15 02:30:07,btc-updown-15m-1794731400,BTC,Up,64512.02,64495.39,894,0.5155,0.45,0.56,0.0482,0,-0.354\n")
        f.write("2026-11-15 02:30:40,hype-updown-15m-1794731400,HYPE,Down,65.4075,65.36,861,0.5395,0.64,0.38,0.064,0,0.27\n")
    up, down = fol.poll()
    assert (up.coin, up.side, up.window_start) == ("btc", "UP", 1794731400)
    assert up.price == pytest.approx(0.45) and up.edge == pytest.approx(0.0482)
    assert (down.coin, down.side) == ("hype", "DOWN") and down.price == pytest.approx(0.38)


def test_seven_signals_same_window_all_go_like_paper(tmp_path):
    """7 monedas a la vez (como las 02:30): entra en las 7, $1 cada una."""
    ex, clk, g, c = make(tmp_path, live=True)
    coins = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]
    recs = ex.handle_signals([sig(clk, k) for k in coins])
    assert [r["moneda"] for r in recs] == [k.upper() for k in coins]
    assert sum(float(r["costo_usd"]) for r in recs) == pytest.approx(7.0)


def test_follow_reads_first_signal_when_csv_created_later(tmp_path):
    path = tmp_path / "senales.csv"
    fol = E.CsvFollower(str(path), {})
    assert fol.poll() == []                                   # aún no existe
    path.write_text("hora,slug,moneda,lado,precio,target,seg_rest,prob_justa_up,up_ask,down_ask,edge,tendencia,libro\n"
                    "2026-11-15 02:30:07,btc-updown-15m-1794731400,BTC,Up,64512.02,64495.39,894,0.5155,0.45,0.56,0.0482,0,-0.354\n")
    [s1] = fol.poll()
    assert s1.coin == "btc" and s1.price == pytest.approx(0.45)


def test_gamma_falls_back_to_events(monkeypatch):
    """Si /markets?slug no trae nada, usa /events?slug (como scanner.py)."""
    class R:
        def __init__(self, d): self.d = d
        def raise_for_status(self): pass
        def json(self): return self.d
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url.rsplit("/", 1)[1])
        if url.endswith("/markets"):
            return R([])
        return R([{"markets": [{"slug": params["slug"], "closed": False}]}])
    g = E.Gamma()
    monkeypatch.setattr(g.s, "get", fake_get)
    m = g.market("btc-updown-15m-1794731400")
    assert m["slug"] == "btc-updown-15m-1794731400" and calls == ["markets", "events"]


# ── 9. Reintentos en paralelo (caso real de las 04:15) ─────
def _book_by_coin(asks):
    def get_order_book(token):
        coin = token.split("-", 1)[0]
        return {"asks": [{"price": str(asks[coin]), "size": "100"}], "bids": [],
                "tick_size": "0.01", "min_order_size": "5", "neg_risk": False}
    return get_order_book


def test_unfilled_signals_do_not_delay_the_rest(tmp_path):
    """BTC y ETH sin nada al tope; DOGE sí. DOGE debe comprarse al instante,
    no 20 s después esperando a que BTC y ETH agoten sus reintentos."""
    ex, clk, g, c = make(tmp_path)                       # DRY_RUN
    c.get_order_book = _book_by_coin({"btc": 0.61, "eth": 0.58, "doge": 0.45})
    t0 = clk.t
    recs = ex.handle_signals([sig(clk, "btc", price=0.53), sig(clk, "eth", price=0.52),
                              sig(clk, "doge", price=0.52)], wait=False)
    doge = [r for r in recs if r["moneda"] == "DOGE"][0]
    assert doge["estado"] == "LLENADA" and clk.t == t0     # sin esperar a nadie
    assert {r["moneda"]: r["estado"] for r in recs if r["moneda"] != "DOGE"} == \
        {"BTC": "REINTENTANDO", "ETH": "REINTENTANDO"}
    for _ in range(12):                                    # el bucle de --follow
        clk.t += 1
        ex.retry_pending()
    assert {r["moneda"]: r["estado"] for r in recs} == \
        {"BTC": "SIN_LLENAR", "ETH": "SIN_LLENAR", "DOGE": "LLENADA"}
    assert ex._retries == []


def test_retry_fills_when_ask_comes_down(tmp_path):
    ex, clk, g, c = make(tmp_path)
    asks = {"btc": 0.60}
    c.get_order_book = _book_by_coin(asks)
    [rec] = ex.handle_signals([sig(clk, "btc", price=0.53)], wait=False)
    assert rec["estado"] == "REINTENTANDO"
    clk.t += 1; ex.retry_pending()
    asks["btc"] = 0.52                                     # baja el ask
    clk.t += 1; ex.retry_pending()
    assert rec["estado"] == "LLENADA" and float(rec["precio_llenado"]) == pytest.approx(0.52)


def test_interrupted_purchase_is_flagged_on_restart(tmp_path):
    ex, clk, g, c = make(tmp_path, best_ask=0.60)
    ex.handle_signals([sig(clk, price=0.53)], wait=False)  # queda REINTENTANDO y "se corta"
    ex2 = E.Executor(clob=c, gamma=g, base_dir=str(tmp_path), now=clk.now, sleep=clk.sleep)
    [o] = ex2.state["orders"]
    assert o["estado"] == "INTERRUMPIDA" and o["resultado"] == "—"
    assert ex2.handle_signals([sig(clk, price=0.53)]) == []   # el mercado sigue usado
