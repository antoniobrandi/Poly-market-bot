#!/usr/bin/env python3
"""
webstatus.py — página mínima para ver el scanner desde el cel y bajar los datos.

Solo arranca si existe la variable DATA_TOKEN (la contraseña). Sin ella no abre nada.
    https://<tu-dominio>.up.railway.app/?t=<DATA_TOKEN>          resumen
    https://<tu-dominio>.up.railway.app/f/papel.json?t=<TOKEN>   descargar un archivo
Solo sirve archivos de la lista FILES; nunca la llave ni nada fuera de /data.
"""
from __future__ import annotations

import hmac
import json
import os
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DATA = os.getenv("SCANNER_DATA_DIR", "/data")
TOKEN = os.getenv("DATA_TOKEN", "")
FILES = ("papel.json", "senales.csv", "mercado.csv", "resultados.csv",
         "ordenes_dry.csv", "ordenes.csv", "executor.log")


def _stats(trades: list[dict], v: int) -> str:
    t = [x for x in trades if x.get("v", 1) == v]
    done = [x for x in t if x.get("result")]
    if not done:
        return f"modelo v{v}: {len(t)} abiertas, 0 cerradas"
    w = sum(x["result"] == "W" for x in done)
    pnl = sum(x["pnl"] for x in done)
    exp = sum(x["fair"] for x in done) / len(done)
    return (f"modelo v{v}: {len(done)} cerradas, {len(t) - len(done)} abiertas, "
            f"{w}/{len(done)} aciertos ({w / len(done):.0%} vs {exp:.0%} esperado), P&L {pnl:+.2f} USD")


def _row(label: str, g: list[dict]) -> str:
    done = [x for x in g if x.get("result")]
    w = sum(x["result"] == "W" for x in done)
    pct = f"{w / len(done):4.0%}" if done else "   —"
    pnl = sum(x["pnl"] for x in done)
    return f"{label:<12}{len(g):>8}{len(done):>9}{w:>8}{len(done) - w:>9}   {pct}  {pnl:>+9.2f}"


HEAD = f"{'':<12}{'señales':>8}{'cerradas':>9}{'ganadas':>8}{'perdidas':>9}  aciertos   P&L USD"


def breakdown(trades: list[dict], v: int) -> list[str]:
    """Por día (últimos 7) y por precio pagado. Es el papel: cuenta TODAS las señales,
    sin límite diario (el límite solo lo aplica el ejecutor)."""
    t = [x for x in trades if x.get("v", 1) == v]
    if not t:
        return []
    out = ["", f"— modelo v{v} por día (papel, sin límite de pérdida) —", HEAD]
    days = sorted({x.get("time", "")[:10] for x in t})[-7:]
    for d in days:
        out.append(_row(d, [x for x in t if x.get("time", "").startswith(d)]))
    out += ["", f"— modelo v{v} por precio pagado —", HEAD]
    for lo, hi in ((0.30, 0.40), (0.40, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 0.71)):
        out.append(_row(f"{lo*100:.0f}–{hi*100:.0f}¢", [x for x in t if lo <= x.get("ask", 0) < hi]))
    return out


def summary() -> str:
    lines = ["SCANNER 15m — estado", ""]
    try:
        with open(os.path.join(DATA, "papel.json")) as f:
            trades = json.load(f)
        lines += [_stats(trades, 2), _stats(trades, 1)]
        lines += breakdown(trades, 2)
    except (OSError, ValueError):
        lines.append("papel.json todavía no existe")
    log = os.path.join(DATA, "executor.log")
    if os.path.exists(log):
        with open(log, errors="replace") as f:
            tail = deque(f, maxlen=25)
        lines += ["", "— últimas líneas del ejecutor (simula dinero real: SÍ aplica el límite de $5) —",
                  *[x.rstrip() for x in tail]]
    sizes = [f"{n}: {os.path.getsize(os.path.join(DATA, n)) // 1024} KB"
             for n in FILES if os.path.exists(os.path.join(DATA, n))]
    lines += ["", "— archivos (/f/<nombre>?t=...) —", *sizes]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", name: str = ""):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if name:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        given = parse_qs(u.query).get("t", [""])[0]
        if not TOKEN or not hmac.compare_digest(given, TOKEN):
            return self._send(403, b"forbidden\n")
        if u.path in ("/", ""):
            return self._send(200, summary().encode())
        if u.path.startswith("/f/"):
            name = u.path[3:]
            path = os.path.join(DATA, name)
            if name in FILES and os.path.exists(path):
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "application/octet-stream", name)
        return self._send(404, b"no existe\n")

    def log_message(self, *a):          # sin ruido en los logs de Railway
        pass


if __name__ == "__main__":
    if not TOKEN:
        print("webstatus: sin DATA_TOKEN, no se abre la página", flush=True)
    else:
        port = int(os.getenv("PORT", "8080"))
        print(f"webstatus: escuchando en el puerto {port}", flush=True)
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
