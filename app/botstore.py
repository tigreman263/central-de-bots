"""Persistência dos bots: tudo na base de dados. Reiniciar o processo retoma exatamente onde ficou."""
import json
from datetime import datetime, timezone

from .engine import Engine, PENDING, RUNNING, PAUSED, STOPPED

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    capital_usdt REAL NOT NULL, params TEXT NOT NULL, rules TEXT NOT NULL,
    grid TEXT, state TEXT NOT NULL DEFAULT '{}', last_ts INTEGER NOT NULL DEFAULT 0,
    command TEXT, warning TEXT NOT NULL DEFAULT '', created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_orders (
    bot_id INTEGER NOT NULL, slot INTEGER NOT NULL, side TEXT NOT NULL,
    price REAL NOT NULL, qty REAL NOT NULL, active_from INTEGER NOT NULL,
    UNIQUE (bot_id, slot, side)
);
CREATE TABLE IF NOT EXISTS bot_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, slot INTEGER NOT NULL,
    side TEXT NOT NULL, price REAL NOT NULL, qty REAL NOT NULL, fee REAL NOT NULL, pnl REAL
);
CREATE TABLE IF NOT EXISTS bot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_equity (bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bot_candles (
    bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    PRIMARY KEY (bot_id, ts)
);
"""


# colunas acrescentadas na v0.3 (bases de dados antigas são migradas no arranque)
MIGRATIONS = {
    "bots": [("mode", "TEXT NOT NULL DEFAULT 'sim'"), ("source", "TEXT NOT NULL DEFAULT 'mainnet'"),
             ("twin_of", "INTEGER")],
    "bot_orders": [("cid", "TEXT"), ("oid", "TEXT"), ("state", "TEXT"), ("otype", "TEXT"),
                   ("exec_qty", "REAL NOT NULL DEFAULT 0"), ("slots", "TEXT")],
}


CANDLE_KEEP_MS = 8 * 24 * 3_600_000


def candles(conn, bot_id, since_ms=0):
    rows = conn.execute("SELECT ts, o, h, l, c FROM bot_candles WHERE bot_id = ? AND ts >= ? ORDER BY ts",
                        (bot_id, since_ms)).fetchall()
    return [tuple(r) for r in rows]


def fills_since(conn, bot_id, since_ms):
    return conn.execute("SELECT * FROM bot_fills WHERE bot_id = ? AND ts >= ? ORDER BY ts", (bot_id, since_ms)).fetchall()


def events_since(conn, bot_id, since_ms):
    return conn.execute("SELECT * FROM bot_events WHERE bot_id = ? AND ts >= ? ORDER BY ts", (bot_id, since_ms)).fetchall()


def init(conn):
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] if hasattr(r, "keys") else r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def create(conn, pair, capital, rules, params=None, mode="sim", source=None, twin_of=None):
    """mode: "sim" (simulador) ou "testnet" (ordens na Testnet). source: de onde vêm as velas (testnet => testnet)."""
    source = source or ("testnet" if mode == "testnet" else "mainnet")
    eng = Engine.new(pair, capital, rules, params, mode)
    cur = conn.execute(
        "INSERT INTO bots (pair, status, capital_usdt, params, rules, created_ts, mode, source, twin_of) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pair, PENDING, capital, json.dumps(eng.p), json.dumps(rules),
         datetime.now(timezone.utc).isoformat(timespec="seconds"), mode, source, twin_of))
    conn.commit()
    return cur.lastrowid


def get(conn, bot_id):
    return conn.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()


def all_bots(conn):
    return conn.execute("SELECT * FROM bots ORDER BY id DESC").fetchall()


def load_engine(conn, bot_id):
    row = get(conn, bot_id)
    if row is None:
        return None
    orders = []
    for o in conn.execute("SELECT * FROM bot_orders WHERE bot_id = ? ORDER BY slot, side", (bot_id,)):
        d = {"slot": o["slot"], "side": o["side"], "price": o["price"], "qty": o["qty"],
             "active_from": o["active_from"]}
        if o["cid"]:                                     # ordem de um bot em modo testnet
            d.update(cid=o["cid"], oid=o["oid"], state=o["state"], type=o["otype"] or "limit",
                     exec_qty=o["exec_qty"] or 0.0)
            if o["slots"]:
                d["slots"] = json.loads(o["slots"])
        orders.append(d)
    eng = Engine({"id": row["id"], "mode": row["mode"], "pair": row["pair"], "status": row["status"], "reason": row["reason"],
                  "capital_usdt": row["capital_usdt"], "params": json.loads(row["params"]),
                  "rules": json.loads(row["rules"]), "grid": json.loads(row["grid"]) if row["grid"] else None,
                  "state": json.loads(row["state"]), "orders": orders, "last_ts": row["last_ts"]})
    return eng


def save_engine(conn, eng, warning=""):
    """Grava o estado inteiro numa só transação: ou fica tudo, ou nada."""
    with conn:
        conn.execute("UPDATE bots SET status=?, reason=?, grid=?, state=?, last_ts=?, warning=? WHERE id=?",
                     (eng.status, eng.reason, json.dumps(eng.grid) if eng.grid else None, json.dumps(eng.s),
                      eng.last_ts, warning, eng.id))
        conn.execute("DELETE FROM bot_orders WHERE bot_id = ?", (eng.id,))
        conn.executemany(
            "INSERT INTO bot_orders (bot_id, slot, side, price, qty, active_from, cid, oid, state, otype, exec_qty, slots) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(eng.id, o["slot"], o["side"], o["price"], o["qty"], o["active_from"], o.get("cid"),
              None if o.get("oid") is None else str(o["oid"]), o.get("state"), o.get("type"),
              o.get("exec_qty", 0.0), json.dumps(o["slots"]) if o.get("slots") else None) for o in eng.orders])
        conn.executemany("INSERT INTO bot_fills (bot_id, ts, slot, side, price, qty, fee, pnl) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         [(eng.id, f["ts"], f["slot"], f["side"], f["price"], f["qty"], f["fee"], f["pnl"]) for f in eng.new_fills])
        conn.executemany("INSERT INTO bot_events (bot_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                         [(eng.id, e["ts"], e["kind"], e["detail"]) for e in eng.new_events])
        conn.executemany("INSERT INTO bot_equity (bot_id, ts, equity) VALUES (?, ?, ?)",
                         [(eng.id, q["ts"], q["equity"]) for q in eng.new_equity])
        conn.executemany("INSERT OR REPLACE INTO bot_candles (bot_id, ts, o, h, l, c) VALUES (?, ?, ?, ?, ?, ?)",
                         [(eng.id, c[0], c[1], c[2], c[3], c[4]) for c in eng.new_candles])
        if eng.new_candles:                               # só guarda os últimos 8 dias de velas por bot
            conn.execute("DELETE FROM bot_candles WHERE bot_id = ? AND ts < ?", (eng.id, eng.new_candles[-1][0] - CANDLE_KEEP_MS))
    eng.new_fills, eng.new_events, eng.new_equity, eng.new_candles = [], [], [], []


def has_pending(conn, row):
    """Um bot testnet já parado pode ainda ter ordens por cancelar ou por enviar (ex.: a fechar a posição)."""
    if row["mode"] != "testnet":
        return False
    if json.loads(row["state"] or "{}").get("cancel_queue"):
        return True
    return conn.execute("SELECT 1 FROM bot_orders WHERE bot_id = ? AND state = 'sending' LIMIT 1",
                        (row["id"],)).fetchone() is not None


def set_command(conn, bot_id, cmd):
    conn.execute("UPDATE bots SET command = ? WHERE id = ? AND status != 'stopped'", (cmd, bot_id))
    conn.commit()


def clear_command(conn, bot_id, expected=None):
    """Apaga o comando pendente; com `expected`, só se ainda for esse (um comando novo do painel nunca se perde)."""
    if expected is None:
        conn.execute("UPDATE bots SET command = NULL WHERE id = ?", (bot_id,))
    else:
        conn.execute("UPDATE bots SET command = NULL WHERE id = ? AND command = ?", (bot_id, expected))
    conn.commit()


def events(conn, bot_id, limit=30):
    return conn.execute("SELECT * FROM bot_events WHERE bot_id = ? ORDER BY id DESC LIMIT ?", (bot_id, limit)).fetchall()


def fills(conn, bot_id, limit=30):
    return conn.execute("SELECT * FROM bot_fills WHERE bot_id = ? ORDER BY id DESC LIMIT ?", (bot_id, limit)).fetchall()


def equity_curve(conn, bot_id, limit=500):
    rows = conn.execute("SELECT ts, equity FROM bot_equity WHERE bot_id = ? ORDER BY ts DESC LIMIT ?",
                        (bot_id, limit)).fetchall()
    return [(r["ts"], r["equity"]) for r in reversed(rows)]


def _max_drawdown_pct(curve, capital):
    """Maior queda (em % do pico) do capital do bot, a partir das leituras horárias."""
    peak, worst = capital, 0.0
    for _, value in curve or []:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak * 100 if peak else 0.0)
    return worst


def stats(eng, now_ms, curve=None):
    """Números para o painel: tudo já líquido de comissões. `curve`: leituras horárias do capital (para o drawdown)."""
    s = eng.s
    if not s or not eng.grid:
        return None
    close = s["last_close"]
    equity = eng.equity(close)
    net = equity - eng.capital
    hours = max(0, (now_ms - s["started_ts"]) / 3_600_000)
    start_price = s.get("start_price")
    hold_pct = (close / start_price - 1) * 100 if start_price else None
    gross = s["realized"] + s["fees"]
    return {
        "equity": equity,
        "net_profit": net,
        "net_pct": (equity / eng.capital - 1) * 100,
        "realized": s["realized"],
        "unrealized": net - s["realized"],                       # o que ainda não foi vendido (ganhos/perdas latentes)
        "fees": s["fees"],
        "fees_pct_of_gross": s["fees"] / gross * 100 if gross > 0 else None,
        "cycles": s["cycles"],
        "cycles_per_day": s["cycles"] / (hours / 24) if hours >= 1 else None,
        "avg_cycle": s["realized"] / s["cycles"] if s["cycles"] else None,
        "wins": s.get("wins", 0),
        "worst_cycle": s["worst_cycle"],
        "worst_loss_pct": s["worst_loss_pct"],
        "limit_loss_pct": eng.p["max_loss_trade_pct"],
        "day_loss_pct": max(0.0, (s["day_start_equity"] - equity) / eng.capital * 100),
        "day_limit_pct": eng.p["daily_loss_pct"],
        "running_hours": hours,
        "inventory_pct": s["base"] * close / eng.capital * 100,
        "open_orders": len(eng.orders),
        "blocked": s["buys_blocked"],
        "hold_pct": hold_pct,                                    # comprar e manter o par, no mesmo período
        "vs_hold_pct": (equity / eng.capital - 1) * 100 - hold_pct if hold_pct is not None else None,
        "max_drawdown_pct": _max_drawdown_pct(curve, eng.capital),
        "stop_distance_pct": (close - eng.grid["stop"]) / close * 100,
        "never_below_cost": bool(eng.p.get("never_sell_below_cost")),
    }
