"""Gráfico de preço de um bot em SVG (feito no servidor, sem bibliotecas): linha do preço, níveis da grelha, limite
inferior/superior e stop, compras e vendas (formas diferentes, não só cor) e eventos. Só desenha; nada aqui decide."""
from datetime import datetime, timezone

WINDOWS = {"6h": 6, "24h": 24, "7d": 168}
MAX_POINTS = 360
GRACE_MS = 120_000    # execuções e eventos dos últimos 2 minutos (ainda sem vela fechada) ficam na ponta direita
EVENT_LABELS = {"setup": "montagem", "pause": "pausa", "recenter": "recentragem", "stop": "stop", "reset": "reset",
                "blocked": "compras suspensas", "guard": "ordem recusada", "error": "erro",
                "recovery": "recuperação", "recon": "reconciliação"}


def _hm(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%d/%m %H:%M")


def _num(x):
    return f"{x:.6g}"


def build(candles, grid, fills, events, orders, window="24h", w=760, h=340):
    """`candles`: [(ts, o, h, l, c)], `fills`/`events`: linhas com ts (ms). Devolve um dicionário para o modelo, ou None."""
    if window not in WINDOWS:
        window = "24h"
    if len(candles) < 2:
        return None
    t1 = candles[-1][0] + 60_000                          # fim da última vela fechada
    t0 = t1 - WINDOWS[window] * 3_600_000
    cs = [c for c in candles if c[0] >= t0] or candles[-2:]
    t0 = max(t0, cs[0][0])

    lows, highs = [c[3] for c in cs], [c[2] for c in cs]
    lo, hi = min(lows), max(highs)
    if grid:
        lo, hi = min(lo, grid["stop"]), max(hi, grid["upper"])
    pad = (hi - lo) * 0.04 or hi * 0.01
    lo, hi = lo - pad, hi + pad
    ml, mr, mt, mb = 56, 74, 14, 30                       # margens (esquerda com preços, direita com rótulos)
    pw, ph = w - ml - mr, h - mt - mb

    def x(ts):
        return ml + min(max(ts - t0, 0), t1 - t0) / ((t1 - t0) or 1) * pw   # nunca fora da área do gráfico

    def y(price):
        return mt + (hi - price) / ((hi - lo) or 1) * ph

    step = max(1, len(cs) // MAX_POINTS)                   # menos pontos, mesmo desenho
    pts = cs[::step]
    if pts[-1] is not cs[-1]:
        pts.append(cs[-1])
    line = " ".join(f"{x(c[0]):.1f},{y(c[4]):.1f}" for c in pts)

    open_by_price = {}
    for o in orders or []:
        if o.get("slot", 0) >= 0:
            open_by_price[round(o["price"], 8)] = o["side"]
    levels = []
    if grid:
        for p in grid["prices"]:
            side = open_by_price.get(round(p, 8))
            levels.append({"y": y(p), "price": _num(p), "side": side})
    marks = []
    for f in fills or []:
        if not t0 <= f["ts"] <= t1 + GRACE_MS:
            continue
        kind = "liq" if f["slot"] == -1 else f["side"]
        marks.append({"x": x(f["ts"]), "y": y(f["price"]), "kind": kind,
                      "tip": f"{'Compra' if f['side'] == 'buy' else 'Venda'}{' (fecho de posição)' if kind == 'liq' else ''}"
                             f" a {_num(f['price'])} · {_num(f['qty'])} · {_hm(f['ts'])}"})
    evs = []
    for e in events or []:
        if t0 <= e["ts"] <= t1 + GRACE_MS and e["kind"] in EVENT_LABELS:
            evs.append({"x": x(e["ts"]), "label": EVENT_LABELS[e["kind"]], "tip": f"{_hm(e['ts'])}: {e['detail']}"})
    ticks = [{"x": x(t0 + (t1 - t0) * k / 4), "label": _hm(t0 + (t1 - t0) * k / 4)} for k in range(5)]
    yticks = [{"y": y(lo + (hi - lo) * k / 4), "label": _num(lo + (hi - lo) * k / 4)} for k in range(5)]
    last = cs[-1][4]
    cs_last_ts = cs[-1][0]
    summary = (f"Preço de {_num(cs[0][4])} a {_num(last)} entre {_hm(t0)} e {_hm(t1)} (UTC), "
               f"{sum(1 for m in marks if m['kind'] == 'buy')} compras e "
               f"{sum(1 for m in marks if m['kind'] in ('sell', 'liq'))} vendas nesta janela.")
    band = None
    if grid:
        band = {"y": y(grid["upper"]), "h": y(grid["lower"]) - y(grid["upper"])}
    return {"w": w, "h": h, "ml": ml, "mr": mr, "mt": mt, "mb": mb, "line": line, "levels": levels, "marks": marks,
            "events": evs, "ticks": ticks, "yticks": yticks, "window": window, "last": (x(cs_last_ts + 60_000), y(last)),
            "last_price": _num(last), "summary": summary, "band": band,
            "lower": {"y": y(grid["lower"]), "label": _num(grid["lower"])} if grid else None,
            "upper": {"y": y(grid["upper"]), "label": _num(grid["upper"])} if grid else None,
            "stop": {"y": y(grid["stop"]), "label": _num(grid["stop"])} if grid else None}
