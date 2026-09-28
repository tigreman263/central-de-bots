"""Persistência dos bots: tudo na base de dados. Reiniciar o processo retoma exatamente onde ficou."""
import json
import secrets
import sqlite3
import time
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
CREATE TABLE IF NOT EXISTS bot_trade_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL, symbol TEXT NOT NULL, order_id TEXT,
    cid TEXT NOT NULL, trade_id TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
    quote_qty REAL NOT NULL, commission REAL NOT NULL DEFAULT 0, commission_asset TEXT, trade_time INTEGER,
    processed_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'booked',
    UNIQUE (bot_id, symbol, cid, trade_id)
);
CREATE INDEX IF NOT EXISTS ix_registry_bot_cid ON bot_trade_registry (bot_id, cid);
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
                   ("exec_qty", "REAL NOT NULL DEFAULT 0"), ("slots", "TEXT"),
                   ("booked_qty", "REAL NOT NULL DEFAULT 0"), ("pending_since", "INTEGER")],
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


def _needs_migration(conn):
    """Uma base de dados que já tem bots mas ainda não tem o registo de trades (ou as colunas novas)."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "bots" not in tables:
        return False                                     # base nova: nada a migrar
    if "bot_trade_registry" not in tables:
        return True
    return "booked_qty" not in {r[1] for r in conn.execute("PRAGMA table_info(bot_orders)")}


def _backup_before_migration(conn):
    """Cópia consistente da base de dados ANTES de a migrar (ao lado do ficheiro). Devolve o caminho, ou None."""
    path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not path:
        return None
    dest = f"{path}.antes-do-registo-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()
    return dest


def init(conn):
    conn.commit()
    if _needs_migration(conn):
        _backup_before_migration(conn)
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] if hasattr(r, "keys") else r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # bots que já existiam: o registo de trades começa agora. O que já foi contabilizado antes fica de fora da procura
    # de execuções por contabilizar (senão contava-se duas vezes).
    now_ms = int(time.time() * 1000)
    for r in conn.execute("SELECT id, state FROM bots WHERE mode = 'testnet'").fetchall():
        st = json.loads(r[1] or "{}")
        if "registry_from" not in st:
            st["registry_from"] = now_ms
            conn.execute("UPDATE bots SET state = ? WHERE id = ?", (json.dumps(st), r[0]))
    conn.commit()


def create(conn, pair, capital, rules, params=None, mode="sim", source=None, twin_of=None):
    """mode: "sim" (simulador) ou "testnet" (ordens na Testnet). source: de onde vêm as velas (testnet => testnet)."""
    source = source or ("testnet" if mode == "testnet" else "mainnet")
    eng = Engine.new(pair, capital, rules, params, mode)
    state = {"uid": secrets.token_hex(3), "registry_from": int(time.time() * 1000)} if mode == "testnet" else {}
    cur = conn.execute(
        "INSERT INTO bots (pair, status, capital_usdt, params, rules, created_ts, mode, source, twin_of, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pair, PENDING, capital, json.dumps(eng.p), json.dumps(rules),
         datetime.now(timezone.utc).isoformat(timespec="seconds"), mode, source, twin_of, json.dumps(state)))
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
            if o["pending_since"] is not None:
                d["pending_since"] = o["pending_since"]
            if o["slots"]:
                d["slots"] = json.loads(o["slots"])
        orders.append(d)
    eng = Engine({"id": row["id"], "mode": row["mode"], "pair": row["pair"], "status": row["status"], "reason": row["reason"],
                  "capital_usdt": row["capital_usdt"], "params": json.loads(row["params"]),
                  "rules": json.loads(row["rules"]), "grid": json.loads(row["grid"]) if row["grid"] else None,
                  "state": json.loads(row["state"]), "orders": orders, "last_ts": row["last_ts"],
                  "seen": _load_registry(conn, bot_id) if row["mode"] == "testnet" else {}})
    return eng


def _load_registry(conn, bot_id):
    """Trades já contabilizados deste bot: id da ordem -> {id do trade: execução} (no formato da Binance)."""
    seen = {}
    for r in conn.execute("SELECT cid, trade_id, qty, price, quote_qty, commission, commission_asset, trade_time "
                          "FROM bot_trade_registry WHERE bot_id = ?", (bot_id,)):
        seen.setdefault(r["cid"], {})[r["trade_id"]] = {
            "id": r["trade_id"], "qty": r["qty"], "price": r["price"], "quoteQty": r["quote_qty"],
            "commission": r["commission"], "commissionAsset": r["commission_asset"], "time": r["trade_time"]}
    return seen


def save_engine(conn, eng, warning=""):
    """Grava o estado inteiro numa só transação: ou fica tudo, ou nada."""
    with conn:
        conn.execute("UPDATE bots SET status=?, reason=?, grid=?, state=?, last_ts=?, warning=? WHERE id=?",
                     (eng.status, eng.reason, json.dumps(eng.grid) if eng.grid else None, json.dumps(eng.s),
                      eng.last_ts, warning, eng.id))
        conn.execute("DELETE FROM bot_orders WHERE bot_id = ?", (eng.id,))
        conn.executemany(
            "INSERT INTO bot_orders (bot_id, slot, side, price, qty, active_from, cid, oid, state, otype, exec_qty, slots, "
            "booked_qty, pending_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(eng.id, o["slot"], o["side"], o["price"], o["qty"], o["active_from"], o.get("cid"),
              None if o.get("oid") is None else str(o["oid"]), o.get("state"), o.get("type"),
              o.get("exec_qty", 0.0), json.dumps(o["slots"]) if o.get("slots") else None,
              eng.booked(o["cid"]) if o.get("cid") else 0.0, o.get("pending_since")) for o in eng.orders])
        conn.executemany(                                   # INSERT simples: um trade repetido falha alto, nunca conta duas vezes
            "INSERT INTO bot_trade_registry (bot_id, symbol, order_id, cid, trade_id, side, qty, price, quote_qty, commission, "
            "commission_asset, trade_time, processed_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(eng.id, t["symbol"], t["order_id"], t["cid"], t["trade_id"], t["side"], t["qty"], t["price"], t["quote_qty"],
              t["commission"], t["commission_asset"], t["trade_time"], t["processed_at"], t["status"])
             for t in eng.new_trades])
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
    eng.new_fills, eng.new_events, eng.new_equity, eng.new_candles, eng.new_trades = [], [], [], [], []


def claims(conn, eng):
    """O que os OUTROS bots da Testnet dizem ter: (moeda deste par, USDT). A conta é uma só: o saldo é de todos."""
    base = cash = 0.0
    for r in conn.execute("SELECT pair, state FROM bots WHERE mode = 'testnet' AND id != ?", (eng.id,)):
        st = json.loads(r["state"] or "{}")
        cash += (st.get("quote") or 0.0) + (st.get("reserve") or 0.0)
        if r["pair"] == eng.pair:
            base += st.get("base") or 0.0
    return base, cash


def link_prefix(conn, bot_id, uid):
    """Liga um UID antigo (de uma cópia de segurança ou de uma base recriada) a este bot: as suas ordens passam a ser
    reconhecidas e limpas na próxima recuperação. Só deve usar-se com um UID que se sabe ser deste bot."""
    eng = load_engine(conn, bot_id)
    if eng is None:
        return False
    eng.link_uid(uid)
    save_engine(conn, eng, "")
    return True


def has_pending(conn, row):
    """Um bot testnet já parado pode ainda ter ordens por cancelar ou por enviar (ex.: a fechar a posição)."""
    if row["mode"] != "testnet":
        return False
    if json.loads(row["state"] or "{}").get("cancel_queue"):
        return True
    return conn.execute("SELECT 1 FROM bot_orders WHERE bot_id = ? AND state IN ('sending', 'settling') LIMIT 1",
                        (row["id"],)).fetchone() is not None


def set_command(conn, bot_id, cmd):
    """Pedido do painel ao bot. Um bot parado só aceita `activate` (o botão ATIVAR)."""
    conn.execute("UPDATE bots SET command = ? WHERE id = ? AND (status != 'stopped' OR ? = 'activate')",
                 (cmd, bot_id, cmd))
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
        "position": eng.position(),                              # total / negociável / residual (pó com custo) / desconhecida
    }


def can_delete(conn, row):
    """Só se apaga um bot que já parou e não tem nada por fechar na exchange."""
    return row["status"] == "stopped" and not has_pending(conn, row)


def delete(conn, bot_id):
    """Apaga o bot e os seus dados (ordens, execuções, eventos, capital, velas e alertas). False se não puder.

    O registo de trades (bot_trade_registry) fica: é auditoria e nunca se apaga."""
    row = get(conn, bot_id)
    if row is None or not can_delete(conn, row):
        return False
    with conn:
        for table in ("bot_orders", "bot_fills", "bot_events", "bot_equity", "bot_candles"):
            conn.execute(f"DELETE FROM {table} WHERE bot_id = ?", (bot_id,))
        conn.execute("UPDATE bots SET twin_of = NULL WHERE twin_of = ?", (bot_id,))
        try:
            conn.execute("DELETE FROM alerts WHERE key LIKE ?", (f"bot:{bot_id}:%",))
        except Exception:
            pass                                   # a tabela de alertas pode ainda não existir
        conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
    return True


def deletable(conn):
    return [r for r in all_bots(conn) if can_delete(conn, r)]
