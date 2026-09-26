"""Staking (Binance Simple Earn): posições lidas e sugestões calculadas por código. Só leitura.

O painel sugere; quem subscreve é o utilizador, na Binance. Nenhuma função aqui subscreve ou resgata.
"""
from datetime import datetime, timezone

MAX_LOCK_SHARE = 50.0      # nunca sugerir prender mais de X% da moeda parada (ponto de partida)
MIN_IDLE_USDT = 5.0        # abaixo disto não vale a pena sugerir
LOCK_PENALTY_PER_30D = 0.5  # pontos de APR descontados por cada 30 dias de bloqueio
RISK_PENALTY = {"alto": 4.0, "atenção": 2.0, "info": 0.5}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _ms(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None


def build_snapshot(reader, held_coins, now=None):
    """Posições (todas) e produtos disponíveis só das moedas que o utilizador tem."""
    held = set(held_coins)
    flex_pos = []
    for r in reader.earn_flexible_positions():
        coin = r["asset"]
        flex_pos.append({"coin": coin, "amount": _f(r["totalAmount"]), "apr": _f(r["latestAnnualPercentageRate"]) * 100,
                         "can_redeem": bool(r.get("canRedeem")), "yesterday_reward": _f(r.get("yesterdayRealTimeRewards")),
                         "total_reward": _f(r.get("cumulativeTotalRewards"))})
    locked_pos = []
    for r in reader.earn_locked_positions():
        locked_pos.append({"coin": r["asset"], "amount": _f(r["amount"]), "apr": _f(r.get("apy")) * 100,
                           "duration": r.get("duration"), "redeem_date": _ms(r.get("deliverDate") or r.get("rewardsEndDate")),
                           "can_redeem_early": bool(r.get("canRedeemEarly")), "auto": bool(r.get("autoSubscribe")),
                           "status": r.get("status")})
    flex_products = {}
    for r in reader.earn_flexible_products():
        if r["asset"] in held and r.get("canPurchase") and not r.get("isSoldOut"):
            apr = _f(r["latestAnnualPercentageRate"]) * 100
            if r["asset"] not in flex_products or apr > flex_products[r["asset"]]["apr"]:
                flex_products[r["asset"]] = {"apr": apr, "min": _f(r.get("minPurchaseAmount"))}
    locked_products = {}
    for r in reader.earn_locked_products():
        d, q = r["detail"], r["quota"]
        if d["asset"] in held and d.get("status") == "PURCHASING" and not d.get("isSoldOut"):
            locked_products.setdefault(d["asset"], []).append(
                {"duration": d["duration"], "apr": _f(d["apr"]) * 100, "min": _f(q.get("minimum")),
                 "renewable": bool(d.get("renewable"))})
    for lst in locked_products.values():
        lst.sort(key=lambda p: -p["apr"])
    return {"ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "flexible": flex_pos, "locked": locked_pos,
            "flex_products": flex_products, "locked_products": locked_products}


def positions_summary(snap, prices):
    """Valor em USDT e rendimento anual estimado por tipo. `prices`: {moeda: preço em USDT}."""
    out = {"flexible": [], "locked": [], "flex_value": 0.0, "locked_value": 0.0, "yearly_usdt": 0.0}
    for kind, key in (("flexible", "flex_value"), ("locked", "locked_value")):
        for p in snap[kind]:
            price = prices.get(p["coin"])
            value = p["amount"] * price if price is not None else None
            yearly = value * p["apr"] / 100 if value is not None else None
            out[kind].append({**p, "value_usdt": value, "yearly_usdt": yearly})
            if value is not None:
                out[key] += value
                out["yearly_usdt"] += yearly
    out["flexible"].sort(key=lambda p: -(p["value_usdt"] or 0))
    out["locked"].sort(key=lambda p: -(p["value_usdt"] or 0))
    return out


def suggestions(snap, holdings, risk_alerts):
    """Sugestões por moeda com saldo parado. O código decide; o texto só explica.

    risk_alerts: {moeda: gravidade mais alta aberta}. Regras de segurança:
    - nunca sugerir bloqueado em moeda com alerta de risco;
    - nunca sugerir prender mais de MAX_LOCK_SHARE% da moeda parada.
    """
    out = []
    for h in holdings:
        if not h.get("priced") or h.get("price") is None:
            continue
        coin = h["coin"]
        idle = max(h["qty"] - h.get("earn", 0.0) - h.get("locked", 0.0), 0.0)  # livre, fora de Earn e de ordens
        idle_value = idle * h["price"]
        if idle_value < MIN_IDLE_USDT:
            continue
        severity = risk_alerts.get(coin)
        penalty = RISK_PENALTY.get(severity, 0.0)
        options = []
        fp = snap["flex_products"].get(coin)
        if fp and idle >= fp["min"]:
            options.append(("Flexível", None, fp["apr"], 100.0, fp["apr"] - penalty))
        if severity not in ("alto", "atenção"):
            for lp in snap["locked_products"].get(coin, []):
                amount_share = MAX_LOCK_SHARE
                if idle * amount_share / 100 >= lp["min"]:
                    score = lp["apr"] - penalty - LOCK_PENALTY_PER_30D * lp["duration"] / 30
                    options.append(("Bloqueado", lp["duration"], lp["apr"], amount_share, score))
        if not options:
            continue
        kind, duration, apr, share, score = max(options, key=lambda o: o[4])
        amount = idle * share / 100
        yearly = amount * h["price"] * apr / 100
        reasons = [f"tens {idle:.6g} {coin} parados (~{idle_value:.2f} USDT)"]
        if kind == "Flexível":
            reasons.append(f"o produto flexível paga {apr:.2f}% APR e podes resgatar quando quiseres")
        else:
            reasons.append(f"o produto bloqueado a {duration} dias paga {apr:.2f}% APR, mas só o resgatas no fim do prazo; "
                           f"sugiro no máximo {share:.0f}% do saldo parado")
        if severity:
            reasons.append(f"a moeda tem um alerta de risco ({severity})")
        out.append({"coin": coin, "kind": kind, "duration": duration, "apr": apr, "amount": amount, "share": share,
                    "yearly_usdt": yearly, "score": score, "risk": severity, "reasons": reasons,
                    "howto": f"Na Binance: Earn → Simple Earn → {kind}, procura {coin} e subscreve {amount:.6g} {coin}."})
    out.sort(key=lambda s: -s["score"])
    return out
