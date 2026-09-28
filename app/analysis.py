"""Análise completa da carteira (aba Ai): tudo por código, cada ponto com o número que o justifica.

Nada aqui decide ou envia ordens. O resumo para uma IA futura sai daqui, sem chaves nem IDs.
"""
import json
import re

SUMMARY_VERSION = 1
END = " A decisão é sua."


def _weighted_change(holdings, key):
    """Variação % do total, ponderada pelo valor de hoje, só com moedas que têm o dado."""
    now = prev = 0.0
    for h in holdings:
        ch = h.get(key)
        if h.get("priced") and ch is not None and h["value_usdt"] and ch > -100:
            now += h["value_usdt"]
            prev += h["value_usdt"] / (1 + ch / 100)
    return (now / prev - 1) * 100 if prev else None


def build(snap, costs_applied, staking_summary, staking_sugs, alerts_open):
    hs = [h for h in snap["holdings"] if h.get("priced")]
    total = snap["total_usdt"] or 0.0
    stable_val = sum(h["value_usdt"] for h in hs if h["stable"])
    earn_val = sum(h.get("earn", 0.0) * h["price"] for h in hs)
    coins = sorted((h for h in hs if not h["stable"]), key=lambda h: -h["value_usdt"])

    blocks = {}
    blocks["desempenho"] = {"change24": _weighted_change(hs, "change24"), "change7d": _weighted_change(hs, "change7d"),
                            "change30d": _weighted_change(hs, "change30d")}
    top3 = coins[:3]
    top3_share = sum(h["weight"] for h in top3)
    hhi = sum((h["weight"] / 100) ** 2 for h in coins)  # 1 = tudo numa moeda; perto de 0 = muito repartido
    blocks["concentracao"] = {"top3": [(h["coin"], h["weight"]) for h in top3], "top3_share": top3_share, "hhi": hhi}
    vols = [(h["value_usdt"], h["vol30"]) for h in coins if h.get("vol30") is not None]
    vol_w = sum(v * s for v, s in vols) / sum(v for v, _ in vols) if vols else None
    blocks["risco"] = {"vol30_ponderada": vol_w, "stable_share": stable_val / total * 100 if total else 0.0,
                       "alertas": len(alerts_open)}
    significant = [h for h in coins if h["weight"] >= 2.0]
    cum, n90 = 0.0, 0
    for h in coins:
        cum += h["weight"]
        n90 += 1
        if cum >= 90:
            break
    blocks["diversificacao"] = {"moedas": len(coins), "moedas_2pct": len(significant), "n_para_90pct": n90,
                                "poeira": sum(1 for h in snap["holdings"] if h.get("dust"))}
    blocks["staking"] = {"earn_share": earn_val / total * 100 if total else 0.0,
                         "locked_value": staking_summary["locked_value"] if staking_summary else None,
                         "yearly_usdt": staking_summary["yearly_usdt"] if staking_summary else None}
    with_cost = [h for h in costs_applied if h.get("pnl_usdt") is not None]
    blocks["lucro_prejuizo"] = {
        "linhas": [(h["coin"], h["cost"], h["price"], h["pnl_usdt"], h["pnl_pct"]) for h in sorted(with_cost, key=lambda h: h["pnl_usdt"])],
        "total": sum(h["pnl_usdt"] for h in with_cost) if with_cost else None,
        "sem_custo": [h["coin"] for h in coins if h.get("cost") is None and not h.get("dust")][:12],
    }

    tips = []  # (importância, texto, número que justifica)

    def tip(importance, text, number):
        tips.append({"importance": importance, "text": text + END, "number": number})

    for h in coins:
        if h["weight"] > 40:
            tip(90, f"{h['coin']} pesa {h['weight']:.1f}% do portefólio. Vê se é o peso que queres numa só moeda.",
                f"{h['weight']:.1f}%")
    for h in with_cost:
        if h["pnl_pct"] is not None and h["pnl_pct"] < -20 and h["value_usdt"] >= 5:
            tip(80, f"{h['coin']} está {abs(h['pnl_pct']):.1f}% abaixo do teu custo médio. Vender agora fixaria o prejuízo; "
                    f"decide se manter é uma escolha tua ou se o preço voltar já não é o teu plano.",
                f"{h['pnl_pct']:.1f}% ({h['pnl_usdt']:.2f} USDT)")
    if staking_sugs:
        top = staking_sugs[0]
        total_y = sum(s["yearly_usdt"] for s in staking_sugs)
        tip(70, f"Tens saldo parado que poderia render. A melhor sugestão é {top['coin']} em {top['kind'].lower()} "
                f"({top['apr']:.2f}% APR). Vê a aba Staking.", f"~{total_y:.2f} USDT/ano se aplicasses todas as sugestões")
    if blocks["risco"]["stable_share"] < 5 and total:
        tip(50, f"Só {blocks['risco']['stable_share']:.1f}% do portefólio está em stablecoins. Sem margem, uma queda "
                f"obriga a decidir sem liquidez.", f"{blocks['risco']['stable_share']:.1f}%")
    elif blocks["risco"]["stable_share"] > 60:
        tip(40, f"{blocks['risco']['stable_share']:.1f}% do portefólio está em stablecoins. Estás muito protegido, "
                f"mas com pouca exposição a subidas.", f"{blocks['risco']['stable_share']:.1f}%")
    if blocks["diversificacao"]["poeira"] >= 5:
        tip(30, f"Tens {blocks['diversificacao']['poeira']} posições minúsculas (menos de 1 USDT). Dão trabalho e comissões "
                f"se as quiseres converter.", f"{blocks['diversificacao']['poeira']} posições")
    liq = [a for a in alerts_open if a["criterion"] == "liquidez"]
    if liq:
        tip(60, f"{len(liq)} moeda(s) têm pouca liquidez ({', '.join(a['coin'] for a in liq[:6])}). Pode ser difícil "
                f"vender ao preço esperado.", f"{len(liq)} moeda(s)")
    tips.sort(key=lambda t: -t["importance"])
    return {"blocks": blocks, "tips": tips, "total_usdt": total}


# ---------- resumo estruturado para uma IA futura ----------
def ai_summary(snap, costs_applied, staking_summary, alerts_open):
    """Resumo versionado da carteira: valores completos, sem chaves, endereços nem IDs de conta.

    Lê de `costs_applied` (não de `snap["holdings"]`): é ali que `costbasis.apply` já juntou o custo médio e o
    lucro/prejuízo não realizado a cada moeda; `snap["holdings"]` nunca tem essa informação.
    """
    coins, dust = [], [h for h in costs_applied if h.get("priced") and h.get("dust")]
    for h in costs_applied:
        if not h.get("priced") or h.get("dust"):
            continue
        coins.append({"coin": h["coin"], "quantity": round(h["qty"], 8), "value_usdt": round(h["value_usdt"], 2),
                      "weight_pct": round(h["weight"], 2), "stable": h["stable"], "in_earn": round(h.get("earn", 0.0), 8),
                      "change_24h_pct": _r(h.get("change24")), "change_7d_pct": _r(h.get("change7d")),
                      "avg_cost_usdt": h.get("cost"), "unrealized_pnl_usdt": _r(h.get("pnl_usdt")),
                      "unrealized_pnl_pct": _r(h.get("pnl_pct"))})
    return {
        "version": SUMMARY_VERSION,
        "generated_at": snap["ts"],
        "total_usdt": round(snap["total_usdt"], 2),
        "holdings": coins,
        "dust": {"positions": len(dust), "value_usdt": round(sum(h["value_usdt"] for h in dust), 2)},
        "staking": ({"flexible_value_usdt": round(staking_summary["flex_value"], 2),
                     "locked_value_usdt": round(staking_summary["locked_value"], 2),
                     "estimated_yearly_reward_usdt": round(staking_summary["yearly_usdt"], 2)} if staking_summary else None),
        "risk_alerts": [{"coin": a["coin"], "criterion": a["criterion"], "severity": a["severity"]} for a in alerts_open],
        "note": "Read-only summary. No API keys, no account ids. The assistant cannot place orders.",
    }


def _r(x):
    return None if x is None else round(x, 2)


LONG_SECRET = re.compile(r"[A-Za-z0-9]{40,}")


def verify_no_secrets(text, secrets):
    """Devolve a lista de problemas; vazia = o resumo pode ser mostrado/enviado."""
    problems = [f"contém um segredo conhecido" for s in secrets if s and s in text]
    if LONG_SECRET.search(text):
        problems.append("contém uma sequência longa que parece uma chave")
    return problems


def summary_text(summary):
    return json.dumps(summary, ensure_ascii=False, indent=2)
