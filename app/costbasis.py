"""Custo médio por moeda, escrito à mão (a API da Binance não o fornece). Moedas sem custo: 'sem custo'."""
from datetime import datetime, timezone

from . import db

FEE = 0.001  # estimativa de comissão ao vender (0,1%)


def load(conn):
    return {r["coin"]: {"avg": r["avg_price_usdt"], "ts": r["updated_ts"]}
            for r in conn.execute("SELECT coin, avg_price_usdt, updated_ts FROM cost_basis")}


def save(conn, coin, avg):
    conn.execute("INSERT INTO cost_basis (coin, avg_price_usdt, updated_ts) VALUES (?, ?, ?) "
                 "ON CONFLICT(coin) DO UPDATE SET avg_price_usdt = excluded.avg_price_usdt, updated_ts = excluded.updated_ts",
                 (coin.upper(), avg, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()


def delete(conn, coin):
    conn.execute("DELETE FROM cost_basis WHERE coin = ?", (coin.upper(),))
    conn.commit()


def parse_price(raw):
    """Aceita '0,75' ou '0.75'. Devolve float > 0 ou None."""
    v = db.parse_decimal_pt(raw)
    return v if v is not None and v > 0 else None


def apply(holdings, costs):
    """Junta custo e lucro/prejuízo não realizado (estimativa) às moedas. Sem custo: nada é inventado."""
    for h in holdings:
        c = costs.get(h["coin"])
        h["cost"] = c["avg"] if c else None
        h["pnl_usdt"] = h["pnl_pct"] = h["pnl_after_fee_usdt"] = None
        if c and h.get("priced") and h.get("price") is not None and not h.get("stable"):
            h["pnl_usdt"] = (h["price"] - c["avg"]) * h["qty"]
            h["pnl_pct"] = (h["price"] / c["avg"] - 1) * 100
            h["pnl_after_fee_usdt"] = h["pnl_usdt"] - FEE * h["value_usdt"]
    return holdings


def loss_warning(h):
    """Texto de aviso se vender agora fixar prejuízo; None caso contrário."""
    if h.get("pnl_after_fee_usdt") is not None and h["pnl_after_fee_usdt"] < 0:
        return (f"Vender {h['coin']} agora fixa um prejuízo de cerca de {abs(h['pnl_after_fee_usdt']):.2f} USDT "
                f"({abs(h['pnl_pct']):.1f}% abaixo do teu custo médio de {h['cost']:g} USDT, comissões estimadas incluídas).")
    return None
