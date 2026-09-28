"""Portão para a fase de dinheiro real (docs/DESIGN.md): 30-60 dias de resultados na Testnet, pelo menos 100
ciclos, e comparação com "comprar e manter". Só junta números REAIS a partir dos bots da Testnet — nunca inventa
nem estima um valor que não venha diretamente do estado gravado de um bot.
"""
from . import botstore

DAYS_TARGET = 30
CYCLES_TARGET = 100


def evaluate(conn, now_ms):
    """Estado do portão: dias a correr, ciclos completados, e a comparação por bot com comprar-e-manter."""
    rows = [r for r in botstore.all_bots(conn) if r["mode"] == "testnet"]
    entries = []
    for row in rows:
        eng = botstore.load_engine(conn, row["id"])
        if eng is None or not eng.grid:
            continue
        st = botstore.stats(eng, now_ms)
        if st is None:
            continue
        entries.append({"id": row["id"], "pair": row["pair"], "status": row["status"],
                        "started_ts": eng.s["started_ts"], "cycles": st["cycles"],
                        "net_pct": st["net_pct"], "hold_pct": st["hold_pct"], "vs_hold_pct": st["vs_hold_pct"]})

    if not entries:
        return {"days": 0.0, "days_target": DAYS_TARGET, "days_ok": False,
                "cycles": 0, "cycles_target": CYCLES_TARGET, "cycles_ok": False,
                "bots": [], "beats_hold": None, "ready": False}

    days = (now_ms - min(e["started_ts"] for e in entries)) / 86_400_000
    cycles = sum(e["cycles"] for e in entries)
    days_ok = days >= DAYS_TARGET
    cycles_ok = cycles >= CYCLES_TARGET
    beats_hold = all(e["vs_hold_pct"] is not None and e["vs_hold_pct"] > 0 for e in entries)
    return {"days": round(days, 1), "days_target": DAYS_TARGET, "days_ok": days_ok,
            "cycles": cycles, "cycles_target": CYCLES_TARGET, "cycles_ok": cycles_ok,
            "bots": entries, "beats_hold": beats_hold, "ready": days_ok and cycles_ok and beats_hold}
