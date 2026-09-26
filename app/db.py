"""Base de dados local (SQLite): configuração, estado, palavra-passe e registo de ações."""
import sqlite3
from datetime import datetime, timedelta, timezone

from .risk import RISK_DEFAULTS

DEFAULTS = {
    "capital_eur": "560",
    "split_reserve": "60",
    "split_trading": "30",
    "split_cash": "10",
    "max_loss_trade": "2",
    "profit_to_reserve": "50",
    "pause_drawdown": "20",
    "exclusions": "memecoins,leveraged",
}

DEFAULTS.update({k: f"{v:.10g}" for k, v in RISK_DEFAULTS.items()})

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cost_basis (
    coin TEXT PRIMARY KEY,
    avg_price_usdt REAL NOT NULL,
    updated_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


AUDIT_KEEP_DAYS = 90


def connect(path):
    """Ligação com espera longa por bloqueios: o painel, o corredor e o vigilante escrevem na mesma base de dados."""
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")     # seguro com WAL e muito menos escritas no cartão SD
    return conn


def init_db(path):
    conn = connect(path)
    conn.execute("PRAGMA journal_mode=WAL")       # leitores e escritor não se bloqueiam (guardado no ficheiro)
    conn.executescript(SCHEMA)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=AUDIT_KEEP_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM audit WHERE ts < ?", (cutoff,))   # o registo de ações não cresce para sempre
    conn.commit()
    conn.close()


def get_all(conn):
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = dict(DEFAULTS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def get(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row:
        return row["value"]
    return DEFAULTS.get(key, default)


def set_many(conn, values):
    conn.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(k, str(v)) for k, v in values.items()],
    )
    conn.commit()


def log(conn, action, detail=""):
    conn.execute(
        "INSERT INTO audit (ts, action, detail) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), action, detail),
    )
    conn.commit()


def recent_log(conn, limit=50):
    return conn.execute(
        "SELECT ts, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
