"""Agente da reserva: propõe moedas estabelecidas para a reserva, com dados reais da Binance. Nunca decide por si —
cada proposta espera Aprovar/Rejeitar. Nunca compra nem move dinheiro: só regista a decisão. A "capitalização de mercado
há vários anos" que o desenho original pedia não vem de nenhum endpoint público fiável da Binance; por isso a lista de
candidatas é fixa e escrita à mão (documentada como tal), e só os números reais (volume, spread) vêm da exchange.
"""
import json
from datetime import datetime, timezone

from . import pairs

# lista fixa de candidatas "estabelecidas": não é uma classificação de mercado em tempo real, é escolhida à mão e
# revista de vez em quando. O código só confirma, com dados reais, que a moeda continua líquida e a negociar.
ESTABLISHED = {
    "BTC": "A criptomoeda mais antiga e de maior capitalização, em negociação desde 2009.",
    "ETH": "A maior rede de contratos inteligentes, em produção desde 2015.",
    "BNB": "Token da maior exchange por volume; longo histórico e liquidez muito alta.",
    "XRP": "Uma das criptomoedas mais antigas, com mais de 10 anos de histórico de negociação.",
    "LTC": "Uma das primeiras alternativas ao Bitcoin, ativa desde 2011.",
    "ADA": "Vários anos de histórico e capitalização consistentemente entre as maiores.",
    "SOL": "Rede de alta atividade com vários anos de histórico e volume elevado.",
    "DOT": "Rede estabelecida focada em interoperabilidade entre blockchains.",
    "AVAX": "Vários anos de atividade e volume alto entre as redes de contratos inteligentes.",
    "LINK": "Rede de oráculos mais usada, com longo histórico de adoção.",
    "TRX": "Uma das redes mais antigas por capitalização, com volume constante.",
    "ATOM": "Rede estabelecida focada em interoperabilidade, vários anos de histórico.",
}

MIN_QUOTE_VOLUME = 100_000_000   # USDT em 24 h — mais exigente que a grelha: só liquidez muito alta ("blue-chip")
MAX_SPREAD_PCT = 0.05
RISK_NOTE = ("Continua a ser uma criptomoeda: o valor pode cair, mesmo sendo uma das mais estabelecidas. A reserva "
             "fica dentro de cripto, não é equivalente a dinheiro parado; e nada é comprado de verdade — isto só "
             "regista a decisão.")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def candidates(tickers, exclusions=()):
    """As candidatas da lista fixa que hoje cumprem, com dados reais, volume alto e spread apertado."""
    out = []
    for coin, why in ESTABLISHED.items():
        if ("memecoins" in exclusions and coin in pairs.MEMECOINS) or \
                ("leveraged" in exclusions and coin.endswith(pairs.LEVERAGED_SUFFIXES)):
            continue
        sym = coin + "USDT"
        t = tickers.get(sym)
        if not t:
            continue
        last, bid, ask = _f(t.get("lastPrice")), _f(t.get("bidPrice")), _f(t.get("askPrice"))
        vol = _f(t.get("quoteVolume"))
        if not (last > 0 and bid > 0 and ask > 0):
            continue
        spread = (ask - bid) / bid * 100
        if vol < MIN_QUOTE_VOLUME or spread > MAX_SPREAD_PCT:
            continue
        out.append({"coin": coin, "pair": sym, "price": last, "quote_volume": vol, "spread_pct": spread, "why": why})
    out.sort(key=lambda c: -c["quote_volume"])
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS reserve_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT NOT NULL, pair TEXT NOT NULL, justification TEXT NOT NULL,
    risk TEXT NOT NULL, suggested_pct REAL NOT NULL, quote_volume REAL, spread_pct REAL,
    status TEXT NOT NULL DEFAULT 'pending', created_ts TEXT NOT NULL, decided_ts TEXT, note TEXT
);
"""


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def known_coins(conn):
    """Moedas pendentes, aprovadas ou já rejeitadas: nunca se repete a proposta. Uma moeda REMOVIDA da reserva pode
    voltar a ser proposta (a remoção é uma decisão sobre tê-la agora na reserva, não sobre nunca mais a considerar)."""
    init(conn)
    return {r[0] for r in conn.execute("SELECT DISTINCT coin FROM reserve_proposals WHERE status != 'removed'")}


def propose(conn, tickers, exclusions=(), limit=3):
    """Cria até `limit` propostas novas. Devolve as que criou (lista de dicts)."""
    init(conn)
    known = known_coins(conn)
    cands = [c for c in candidates(tickers, exclusions) if c["coin"] not in known][:limit]
    approved_n = conn.execute("SELECT COUNT(*) FROM reserve_proposals WHERE status = 'approved'").fetchone()[0]
    pending_n = conn.execute("SELECT COUNT(*) FROM reserve_proposals WHERE status = 'pending'").fetchone()[0]
    created = []
    for i, c in enumerate(cands):
        pct = round(100 / (approved_n + pending_n + i + 2), 1)          # peso indicativo, em partes iguais
        row = {"coin": c["coin"], "pair": c["pair"], "justification": c["why"], "risk": RISK_NOTE,
               "suggested_pct": pct, "quote_volume": c["quote_volume"], "spread_pct": c["spread_pct"],
               "status": "pending", "created_ts": _now()}
        conn.execute("INSERT INTO reserve_proposals (coin, pair, justification, risk, suggested_pct, quote_volume, "
                     "spread_pct, status, created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                     (row["coin"], row["pair"], row["justification"], row["risk"], row["suggested_pct"],
                      row["quote_volume"], row["spread_pct"], row["status"], row["created_ts"]))
        created.append(row)
    conn.commit()
    return created


def decide(conn, proposal_id, approve, note=""):
    """Aprova ou rejeita uma proposta pendente. Devolve a linha (coin, status) ou None se não existir/já decidida."""
    init(conn)
    row = conn.execute("SELECT * FROM reserve_proposals WHERE id = ? AND status = 'pending'", (proposal_id,)).fetchone()
    if row is None:
        return None
    status = "approved" if approve else "rejected"
    conn.execute("UPDATE reserve_proposals SET status = ?, decided_ts = ?, note = ? WHERE id = ?",
                 (status, _now(), note, proposal_id))
    conn.commit()
    return {"coin": row["coin"], "status": status}


def remove(conn, proposal_id, note=""):
    """Retira uma moeda já aprovada da reserva. Fica no histórico como 'removida'; nunca se apaga o registo."""
    init(conn)
    row = conn.execute("SELECT * FROM reserve_proposals WHERE id = ? AND status = 'approved'", (proposal_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE reserve_proposals SET status = 'removed', decided_ts = ?, note = ? WHERE id = ?",
                 (_now(), note, proposal_id))
    conn.commit()
    return {"coin": row["coin"], "status": "removed"}


def adjust(conn, proposal_id, pct):
    """Muda o peso-alvo de uma moeda já aprovada (nunca dos pendentes: essas ainda estão por decidir)."""
    init(conn)
    if not (0 < pct <= 100):
        return None
    row = conn.execute("SELECT * FROM reserve_proposals WHERE id = ? AND status = 'approved'", (proposal_id,)).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE reserve_proposals SET suggested_pct = ? WHERE id = ?", (pct, proposal_id))
    conn.commit()
    return {"coin": row["coin"], "pct": pct}


def with_targets(approved, snapshot, split_reserve_pct):
    """Junta a cada moeda aprovada o alvo em USDT (se houver portefólio real ligado) e o que realmente tens hoje.

    "held_usdt" conta só a parte LIVRE (qty - locked): moeda bloqueada numa ordem aberta na Binance ainda pode
    mudar de mãos, por isso não é "segura" na reserva. O valor bloqueado aparece à parte em "locked_usdt", para
    decidires se vale a pena cancelar essa ordem e passar o dinheiro para a reserva.

    Nunca inventa um valor: sem chave ligada, todos estes campos ficam None (mostrados como "—").
    """
    holdings = {h["coin"]: h for h in (snapshot or {}).get("holdings", [])} if snapshot else {}
    reserve_total = snapshot["total_usdt"] * split_reserve_pct / 100 if snapshot else None
    out = []
    for r in approved:
        h = holdings.get(r["coin"])
        if h and h.get("value_usdt") is not None and h["qty"]:
            price = h["value_usdt"] / h["qty"]                            # preço implícito, sem depender de outro campo
            locked_qty = h.get("locked", 0.0)
            held_usdt = (h["qty"] - locked_qty) * price
            locked_usdt = locked_qty * price
        elif h:
            held_usdt = None
            locked_usdt = None
        else:
            held_usdt = 0.0 if snapshot else None
            locked_usdt = 0.0 if snapshot else None
        target_usdt = reserve_total * r["suggested_pct"] / 100 if reserve_total is not None else None
        gap = (target_usdt - held_usdt) if (target_usdt is not None and held_usdt is not None) else None
        held_pct = (held_usdt / reserve_total * 100) if (reserve_total and held_usdt is not None) else None
        out.append({**r, "held_qty": h["qty"] if h else (0.0 if snapshot else None), "held_usdt": held_usdt,
                    "locked_usdt": locked_usdt, "held_pct_of_reserve": held_pct,
                    "target_usdt": target_usdt, "gap_usdt": gap})
    return out


def view(conn, snapshot=None, split_reserve_pct=0.0):
    init(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM reserve_proposals ORDER BY id DESC")]
    approved = [r for r in rows if r["status"] == "approved"]
    pending = [r for r in rows if r["status"] == "pending"]
    decided_history = [r for r in rows if r["status"] not in ("pending", "approved")]
    total_pct = sum(r["suggested_pct"] for r in approved)
    return {"approved": with_targets(approved, snapshot, split_reserve_pct), "pending": pending,
            "history": decided_history, "reserve_coins": sorted(r["coin"] for r in approved),
            "total_pct": round(total_pct, 1), "has_real_portfolio": snapshot is not None}
