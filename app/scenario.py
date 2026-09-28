"""Laboratório de Cenários: corre o motor real de grelha (engine.py) contra um percurso de mercado sintético que o
utilizador desenha (fases de alta/lateral/baixa, com força e duração à escolha), para sentir como as proteções de
risco reagem antes de confiar dinheiro a sério. Um ensaio nunca conta para o portão da conta real, as estatísticas
nem a capacidade do sistema: vive na sua própria tabela, nunca na tabela `bots`.

As velas são determinísticas pela seed: a mesma seed com o mesmo guião produz sempre o mesmo percurso, para se poder
repetir e comparar ensaios.
"""
import json
import random
from datetime import datetime, timezone

from .engine import Engine

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenario_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, pair TEXT NOT NULL, capital REAL NOT NULL,
    rules TEXT NOT NULL, params TEXT NOT NULL, script TEXT NOT NULL,
    seed TEXT NOT NULL, bands TEXT NOT NULL, result TEXT NOT NULL,
    created_ts TEXT NOT NULL
);
"""

DIR = {"alta": 1, "lateral": 0, "baixa": -1}
UNIT_TO_MIN = {"minutos": 1, "horas": 60, "dias": 1440}
PRESETS = {"suave": 1.5, "moderada": 5.0, "forte": 15.0}   # deriva %/dia (magnitude; o sinal vem de DIR)
VOL_PCT_PER_MIN = 0.06     # desvio-padrão do ruído por minuto (%): dá textura de mercado real, mesmo em lateral


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def new_seed():
    return str(random.randint(100_000, 999_999))


def _drift_pct_per_day(phase):
    if phase.get("custom") is not None:
        return phase["custom"]
    return PRESETS.get(phase.get("forca"), PRESETS["moderada"])


def generate_candles(script, seed, start_price, now_ms):
    """Velas de 1 minuto determinísticas pela seed. Devolve (candles, bands):
    candles = [(ts, o, h, l, c)] · bands = [{"start_ts", "end_ts", "tipo"}], uma por fase (para sombrear o gráfico)."""
    rng = random.Random(seed)
    candles, bands, price, ts = [], [], start_price, now_ms
    for phase in script:
        mins = max(1, round(phase["dur"] * UNIT_TO_MIN[phase["unidade"]]))
        drift_per_min = _drift_pct_per_day(phase) * DIR[phase["tipo"]] / 1440 / 100
        start_ts = ts
        for _ in range(mins):
            o = price
            price = max(price * (1 + drift_per_min + rng.gauss(0, VOL_PCT_PER_MIN / 100)), 1e-8)
            wick = abs(rng.gauss(0, VOL_PCT_PER_MIN / 200))
            h, l = max(o, price) * (1 + wick), min(o, price) * (1 - wick)
            candles.append((ts, o, h, l, price))
            ts += 60_000
        bands.append({"start_ts": start_ts, "end_ts": ts, "tipo": phase["tipo"]})
    return candles, bands


def run_engine(pair, capital, rules, params, candles):
    """Corre o motor real (engine.py) sobre as velas sintéticas, do início ao fim (ou até o bot parar sozinho)."""
    eng = Engine.new(pair, capital, rules, params, mode="sim")
    for c in candles:
        eng.process_candle(c)
    final_close = candles[-1][4] if candles else None
    final_equity = eng.equity(final_close) if eng.grid and final_close is not None else capital
    final_pct = (final_equity - capital) / capital * 100 if capital else 0.0
    protections = sorted({e["kind"] for e in eng.new_events} & {"pause", "stop", "blocked"})
    return {"status": eng.status, "reason": eng.reason, "final_equity": final_equity, "final_pct": final_pct,
            "cycles": eng.s.get("cycles", 0), "worst_loss_pct": eng.s.get("worst_loss_pct", 0.0),
            "protections": protections, "equity": eng.new_equity, "events": eng.new_events,
            "fills": eng.new_fills, "orders": eng.orders, "grid": eng.grid, "candles": [list(c) for c in candles]}


def save(conn, name, pair, capital, rules, params, script, seed, bands, result):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "INSERT INTO scenario_runs (name, pair, capital, rules, params, script, seed, bands, result, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, pair, capital, json.dumps(rules), json.dumps(params), json.dumps(script), seed,
             json.dumps(bands), json.dumps(result), now))
    return cur.lastrowid


def get(conn, run_id):
    row = conn.execute("SELECT * FROM scenario_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "pair": row["pair"], "capital": row["capital"],
            "rules": json.loads(row["rules"]), "params": json.loads(row["params"]), "script": json.loads(row["script"]),
            "seed": row["seed"], "bands": json.loads(row["bands"]), "result": json.loads(row["result"]),
            "created_ts": row["created_ts"]}


def list_runs(conn):
    return [dict(r) for r in
            conn.execute("SELECT id, name, pair, seed, created_ts FROM scenario_runs ORDER BY id DESC")]


def delete(conn, run_id):
    with conn:
        conn.execute("DELETE FROM scenario_runs WHERE id = ?", (run_id,))
