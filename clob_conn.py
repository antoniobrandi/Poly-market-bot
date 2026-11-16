"""
clob_conn.py — conexión y firma del CLOB V2 de Polymarket, reutilizable.

Misma configuración que OrderExecutor en polymarket_agent.py:
  · proxy wallet (funder = POLYMARKET_PROXY_ADDRESS, signature_type=2)
  · sin proxy → EOA directo (signature_type=None → 0)
  · credenciales de API del .env; si faltan, se derivan EN MEMORIA

Pensado para que otros programas (p. ej. scanner15/executor.py) operen con
la misma cuenta SIN copiar las llaves: el .env se lee en su lugar, como
diccionario, sin tocar os.environ y sin imprimir ningún secreto.

Este módulo no tiene efectos secundarios al importarse (no configura logging,
no llama load_dotenv, no importa anthropic/flask).
"""
from __future__ import annotations

import os
from typing import Optional

from dotenv import dotenv_values

CLOB_HOST = "https://clob.polymarket.com"
BOT_DIR = os.path.dirname(os.path.abspath(__file__))


_KEYS = ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_PROXY_ADDRESS", "PROXY_ADDRESS",
         "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE")


def load_bot_env(bot_dir: Optional[str] = None) -> dict:
    """Lee el .env del bot como dict (no lo copia ni lo exporta). Sin .env (servidor),
    toma esas mismas variables del entorno, p. ej. las variables secretas de Railway."""
    path = os.path.join(bot_dir or BOT_DIR, ".env")
    if os.path.exists(path):
        return {k: (v or "") for k, v in dotenv_values(path).items()}
    env = {k: os.environ[k] for k in _KEYS if os.environ.get(k)}
    if not env:
        raise FileNotFoundError(f"No existe {path} ni variables POLYMARKET_* en el entorno")
    return env


def make_public_client():
    """Cliente sin llave: solo lectura (orderbook, tick size). Para DRY_RUN."""
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.constants import POLYGON
    return ClobClient(host=CLOB_HOST, chain_id=POLYGON)


def make_trading_client(bot_dir: Optional[str] = None, env: Optional[dict] = None):
    """
    Cliente autenticado (L2) con la firma del bot. Lanza RuntimeError con un
    mensaje claro si falta algo; nunca incluye la llave en el mensaje.
    """
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds
    from py_clob_client_v2.constants import POLYGON

    env = env if env is not None else load_bot_env(bot_dir)
    key = env.get("POLYMARKET_PRIVATE_KEY", "")
    if not key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY vacío en el .env del bot")
    proxy = env.get("POLYMARKET_PROXY_ADDRESS") or env.get("PROXY_ADDRESS") or None

    client = ClobClient(
        host=CLOB_HOST,
        chain_id=POLYGON,
        key=key,
        funder=proxy,
        signature_type=2 if proxy else None,
    )

    if env.get("POLYMARKET_API_KEY"):
        client.set_api_creds(ApiCreds(
            api_key=env["POLYMARKET_API_KEY"],
            api_secret=env.get("POLYMARKET_API_SECRET", ""),
            api_passphrase=env.get("POLYMARKET_API_PASSPHRASE", ""),
        ))
    else:
        creds = None
        try:
            creds = client.create_or_derive_api_key()
        except Exception:
            creds = client.derive_api_key()
        if not creds:
            raise RuntimeError("No se pudieron derivar credenciales de API del CLOB")
        client.set_api_creds(creds)  # solo en memoria; no se imprimen

    return client


def describe_wallet(env: dict) -> str:
    """Texto seguro para logs: modo de firma y dirección abreviada."""
    proxy = env.get("POLYMARKET_PROXY_ADDRESS") or env.get("PROXY_ADDRESS") or ""
    if proxy:
        return f"proxy wallet {proxy[:6]}…{proxy[-4:]} (signature_type=2)"
    return "EOA directo (signature_type=0)"
