"""Organização do Portefólio: cartões de resumo, secções, ordenação, filtros e selos de risco."""
from . import analysis
from .risk import CRITERIA_NAMES, RANK

SORTS = {"valor": ("Valor", lambda h: -(h["value_usdt"] or 0)),
         "variacao": ("Variação 24 h", lambda h: -(h.get("change24") if h.get("change24") is not None else -1e9)),
         "pnl": ("Lucro/prejuízo", lambda h: (h["pnl_usdt"] if h.get("pnl_usdt") is not None else 1e18)),
         "nome": ("Nome", lambda h: h["coin"])}
FILTERS = {"": "Tudo", "alertas": "Só com alertas", "earn": "Só com Earn"}
EARN_SECTION_SHARE = 0.5  # uma moeda vai para a secção Earn se pelo menos metade está em Earn

SECTION_TITLES = [("principais", "Moedas principais"), ("earn", "Earn"), ("stables", "Stablecoins e moeda"),
                  ("poeira", "Poeira (menos de 1 USDT)"), ("sem_preco", "Sem preço")]


def badges_by_coin(open_alerts):
    out = {}
    for a in open_alerts:
        if a["criterion"] == "ligacao":
            continue
        out.setdefault(a["coin"], []).append({"label": CRITERIA_NAMES.get(a["criterion"], a["criterion"]),
                                              "severity": a["severity"]})
    for lst in out.values():
        lst.sort(key=lambda b: -RANK[b["severity"]])
    return out


def _section_of(h):
    if not h.get("priced"):
        return "sem_preco"
    if h.get("dust"):
        return "poeira"
    if h.get("stable") or h["coin"] == "EUR":
        return "stables"
    if h["qty"] > 0 and h.get("earn", 0.0) / h["qty"] >= EARN_SECTION_SHARE:
        return "earn"
    return "principais"


def build(snap, holdings, rate, sort="valor", flt="", show_dust=False, alerts_open=()):
    """`holdings` já com custo aplicado. `rate`: fator USDT→moeda escolhida (None se não houver)."""
    badges = badges_by_coin(alerts_open)
    total = snap["total_usdt"] or 0.0
    stable_val = sum(h["value_usdt"] for h in holdings if h.get("priced") and (h["stable"] or h["coin"] == "EUR"))
    earn_val = sum(h.get("earn", 0.0) * h["price"] for h in holdings if h.get("priced"))
    pnl_rows = [h for h in holdings if h.get("pnl_usdt") is not None]
    cards = {
        "total": total * rate if rate else None,
        "change24": analysis._weighted_change([h for h in holdings if h.get("priced")], "change24"),
        "pnl_total": (sum(h["pnl_usdt"] for h in pnl_rows) * rate) if (pnl_rows and rate) else None,
        "pnl_coins": len(pnl_rows),
        "stable_pct": stable_val / total * 100 if total else 0.0,
        "earn_pct": earn_val / total * 100 if total else 0.0,
        "alerts": sum(len(v) for v in badges.values()),
    }
    key = SORTS.get(sort, SORTS["valor"])[1]
    sections = {sid: [] for sid, _ in SECTION_TITLES}
    for h in sorted(holdings, key=key):
        row = {**h, "shown": (h["value_usdt"] * rate) if (h.get("value_usdt") is not None and rate) else None,
               "pnl_shown": (h["pnl_usdt"] * rate) if (h.get("pnl_usdt") is not None and rate) else None,
               "badges": badges.get(h["coin"], [])}
        if flt == "alertas" and not row["badges"]:
            continue
        if flt == "earn" and not row.get("earn"):
            continue
        sections[_section_of(h)].append(row)
    out = []
    for sid, title in SECTION_TITLES:
        rows = sections[sid]
        if not rows:
            continue
        value = sum(r["value_usdt"] or 0 for r in rows) * (rate or 0)
        out.append({"id": sid, "title": title, "rows": rows, "value": value if rate else None,
                    "hidden": sid == "poeira" and not show_dust})
    return {"cards": cards, "sections": out}
