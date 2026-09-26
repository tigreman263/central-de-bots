"""Preços reais da Binance (endpoint público, só leitura, sem chaves)."""
import json
import time
import urllib.parse
import urllib.request

BASE = "https://api.binance.com/api/v3/ticker/price"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "EURUSDT"]
TTL = 15

_cache = {"t": 0.0, "data": None}


def _download(symbols):
    query = urllib.parse.urlencode({"symbols": json.dumps(symbols, separators=(",", ":"))})
    with urllib.request.urlopen(f"{BASE}?{query}", timeout=5) as resp:
        rows = json.load(resp)
    return {r["symbol"]: float(r["price"]) for r in rows}


def get_market(fetch=_download):
    """Devolve {'ok', 'prices', 'eur_per_usdt'}. Sem ligação: ok=False, nunca números inventados."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["t"] < TTL:
        return _cache["data"]
    try:
        prices = fetch(SYMBOLS)
        usdt_per_eur = prices["EURUSDT"]
        data = {"ok": True, "prices": prices, "eur_per_usdt": 1 / usdt_per_eur}
    except Exception:
        data = {"ok": False, "prices": {}, "eur_per_usdt": None}
    _cache.update(t=now, data=data)
    return data


def clear_cache():
    _cache.update(t=0.0, data=None)
