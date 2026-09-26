"""Constrói o retrato do portefólio a partir das leituras da Binance. Nunca inventa valores."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD"}
DUST_USDT = 1.0


def _f(x):
    return float(x) if x not in (None, "") else None


def _ticker_data(coin, tickers):
    """Preço, volume e spread a partir dos tickers já lidos (sem pedidos de rede)."""
    t = tickers.get(f"{coin}USDT")
    if not t:
        return {}
    data = {"price": _f(t.get("lastPrice")), "change24": _f(t.get("priceChangePercent")),
            "quote_volume": _f(t.get("quoteVolume"))}
    bid, ask = _f(t.get("bidPrice")), _f(t.get("askPrice"))
    if bid and ask and bid > 0:
        data["spread_pct"] = (ask - bid) / bid * 100
    return data


def _history_data(reader, coin):
    """Histórico e estado (pedidos de rede). Se um pedido falhar, esse campo fica vazio: sem estimativas."""
    symbol = f"{coin}USDT"
    data = {}
    try:
        k = reader.klines(symbol)
        closes = [float(c[4]) for c in k]
        opens = [float(c[1]) for c in k]
        data["age_days"] = len(k) if len(k) < 90 else 90
        last30 = list(zip(opens, closes))[-30:]
        if last30:
            data["vol30"] = sum(abs(c / o - 1) * 100 for o, c in last30 if o) / len(last30)
        if len(closes) >= 8 and closes[-8]:
            data["change7d"] = (closes[-1] / closes[-8] - 1) * 100
        if len(closes) >= 31 and closes[-31]:
            data["change30d"] = (closes[-1] / closes[-31] - 1) * 100
    except Exception:
        pass
    try:
        data["status"] = reader.symbol_status(symbol)
    except Exception:
        pass
    return data


def build_snapshot(reader, now=None):
    """Lê a conta e devolve o retrato. Se a leitura da conta falhar, levanta a exceção (quem chama trata)."""
    balances = reader.account()
    tickers = reader.tickers_all()
    eur_price = _f(tickers["EURUSDT"].get("lastPrice"))  # USDT por 1 EUR

    # Binance Earn (poupança flexível) aparece como LD<moeda>: junta-se à moeda base.
    merged = {}
    for b in balances:
        qty, locked = float(b["free"]) + float(b["locked"]), float(b["locked"])
        if qty <= 0:
            continue
        asset, earn = b["asset"], 0.0
        if asset.startswith("LD") and f"{asset}USDT" not in tickers and f"{asset[2:]}USDT" in tickers:
            asset, earn = asset[2:], qty
        m = merged.setdefault(asset, {"qty": 0.0, "locked": 0.0, "earn": 0.0})
        m["qty"] += qty
        m["locked"] += locked
        m["earn"] += earn

    holdings = []
    for coin, m in merged.items():
        qty, locked = m["qty"], m["locked"]
        stable = coin in STABLES
        h = {"coin": coin, "qty": qty, "locked": locked, "earn": m["earn"], "stable": stable, "priced": False}
        if coin == "USDT":
            h.update(price=1.0, priced=True, change24=0.0)
        elif coin == "EUR":
            h.update(price=eur_price, priced=eur_price is not None)
        else:
            d = _ticker_data(coin, tickers)
            h.update(d)
            h["priced"] = d.get("price") is not None
            if stable and h["priced"]:
                h["stable_dev_pct"] = abs(d["price"] - 1) * 100
        h["value_usdt"] = qty * h["price"] if h["priced"] else None
        holdings.append(h)

    total = sum(h["value_usdt"] for h in holdings if h["priced"])
    for h in holdings:
        h["weight"] = (h["value_usdt"] / total * 100) if h["priced"] and total else 0.0
        h["dust"] = h["priced"] and h["value_usdt"] < DUST_USDT
    holdings.sort(key=lambda h: h["value_usdt"] or 0, reverse=True)

    # Histórico e estado só para moedas que valem a pena (não poeira, não stablecoins), em paralelo.
    deep = [h for h in holdings if h["priced"] and not h["dust"] and not h["stable"] and h["coin"] != "EUR"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for h, extra in zip(deep, pool.map(lambda x: _history_data(reader, x["coin"]), deep)):
            h.update(extra)
    return {
        "ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "total_usdt": total,
        "eur_per_usdt": 1 / eur_price if eur_price else None,
        "holdings": holdings,
    }
