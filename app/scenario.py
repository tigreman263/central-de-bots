"""Laboratório de Cenários: testa a reação de um bot a um percurso de mercado escolhido por ti (alta/baixa/lateral),
sem tocar na Binance nem na Testnet. Só o motor de grelha real (engine.py) corre por cima de velas sintéticas —
o código testado é o mesmo que corre um bot a sério, só a fonte das velas muda.

Isolado de propósito: fica na sua própria tabela, nunca na tabela `bots` — um ensaio nunca conta para o portão da
conta real, para as estatísticas nem para a capacidade do sistema.
"""
import json
import random
import secrets
import time
from datetime import datetime, timezone

from .engine import Engine

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenario_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pair TEXT NOT NULL,
    capital REAL NOT NULL,
    rules TEXT NOT NULL,
    params TEXT NOT NULL,
    script TEXT NOT NULL,
    seed TEXT NOT NULL,
    result TEXT NOT NULL,
    created_ts TEXT NOT NULL
);
"""

# deriva %/dia por força; "custom" (escrita à mão) substitui isto por completo, não se combina com o preset
PRESETS = {"suave": 0.4, "moderada": 1.2, "forte": 2.6}
DIR = {"alta": 1, "baixa": -1, "lateral": 0}
UNIT_TO_MIN = {"minutos": 1, "horas": 60, "dias": 1440}


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def force_pct(forca, custom):
    """%/dia de deriva desta fase: um valor escrito à mão vence sempre o preset."""
    return float(custom) if custom is not None else PRESETS.get(forca, PRESETS["moderada"])


def phase_minutes(dur, unidade):
    return max(1, int(round(float(dur) * UNIT_TO_MIN.get(unidade, 1))))


def generate_candles(script, seed, start_price, start_ts=None):
    """Velas de 1 min determinísticas a partir do guião. A mesma seed + o mesmo guião dão sempre as mesmas velas.

    `script`: lista de {"tipo": "alta"|"baixa"|"lateral", "forca": "suave"|"moderada"|"forte", "custom": float|None,
    "dur": float, "unidade": "minutos"|"horas"|"dias"}. Devolve (velas, bandas) — velas prontas para
    `Engine.process_candle`, bandas para desenhar o gráfico por fase.
    """
    rnd = random.Random(seed)
    price = start_price
    ts = start_ts if start_ts is not None else int(time.time() * 1000)
    candles, bands = [], []
    for phase in script:
        dur_min = phase_minutes(phase.get("dur", 1), phase.get("unidade", "horas"))
        drift_daily = force_pct(phase.get("forca"), phase.get("custom")) / 100
        d = DIR.get(phase.get("tipo"), 0)
        band_start = ts
        for _ in range(dur_min):
            o = price
            drift = d * drift_daily / 1440
            # ruído calibrado para a deriva de "forte" (2.6%/dia) se ver com confiança numa fase de poucas horas;
            # "suave" só se nota mesmo em fases mais longas (dias) — como no mercado real, uma deriva pequena
            # perde-se no ruído de curto prazo
            noise = rnd.uniform(-0.00018, 0.00018) if d else rnd.uniform(-0.0007, 0.0007)
            c = max(1e-8, o * (1 + drift + noise))
            h = max(o, c) * (1 + rnd.uniform(0, 0.0006))
            l = min(o, c) * (1 - rnd.uniform(0, 0.0006))
            candles.append((ts, o, h, l, c))
            price = c
            ts += 60_000
        bands.append((band_start, ts, phase.get("tipo")))
    return candles, bands


def run_engine(pair, capital, rules, params, candles):
    """Corre o motor real (engine.py) sobre as velas sintéticas. Nunca envia nada a lado nenhum."""
    eng = Engine.new(pair, capital, rules, params, mode="sim")
    for candle in candles:
        eng.process_candle(candle)
    peak, max_dd = capital, 0.0
    for pt in eng.new_equity:
        peak = max(peak, pt["equity"])
        if peak:
            max_dd = max(max_dd, (peak - pt["equity"]) / peak * 100)
    last_equity = eng.new_equity[-1]["equity"] if eng.new_equity else capital
    final_pct = (last_equity - capital) / capital * 100 if capital else 0.0
    return {
        "status": eng.status, "reason": eng.reason,
        "events": eng.new_events, "fills": eng.new_fills, "equity": eng.new_equity,
        "cycles": eng.s.get("cycles", 0), "stop_events": eng.s.get("stop_events", 0),
        "final_pct": final_pct, "max_dd": max_dd,
    }


def save(conn, name, pair, capital, rules, params, script, seed, bands, result):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO scenario_runs (name, pair, capital, rules, params, script, seed, result, created_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (name, pair, capital, json.dumps(rules), json.dumps(params), json.dumps(script), seed,
         json.dumps({**result, "bands": bands}), now))
    conn.commit()
    return cur.lastrowid


def list_runs(conn):
    rows = conn.execute("SELECT id, name, pair, capital, seed, script, result, created_ts "
                        "FROM scenario_runs ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["script"] = json.loads(d["script"])
        result = json.loads(d.pop("result"))
        d["final_pct"] = result["final_pct"]
        d["max_dd"] = result["max_dd"]
        d["stop_events"] = result["stop_events"]
        out.append(d)
    return out


def get(conn, run_id):
    row = conn.execute("SELECT * FROM scenario_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["rules"] = json.loads(d["rules"])
    d["params"] = json.loads(d["params"])
    d["script"] = json.loads(d["script"])
    d["result"] = json.loads(d["result"])
    return d


def delete(conn, run_id):
    conn.execute("DELETE FROM scenario_runs WHERE id = ?", (run_id,))
    conn.commit()


def new_seed():
    return "malha-" + secrets.token_hex(3)
