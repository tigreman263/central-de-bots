"""Alertas do portefólio: chave única (moeda + critério), sem repetição em 24 h, fecham sozinhos."""
from datetime import datetime, timedelta, timezone

from .risk import HIGH, INFO, RANK

REPEAT = timedelta(hours=24)
CONNECTION_KEY = "binance:ligacao"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    coin TEXT NOT NULL,
    criterion TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    first_ts TEXT NOT NULL,
    surfaced_ts TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0
);
"""


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(s):
    return datetime.fromisoformat(s)


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def sync(conn, findings, now=None):
    """Aplica os riscos encontrados. Devolve a lista de chaves que ficaram visíveis de novo."""
    now = now or datetime.now(timezone.utc)
    current = {f["key"] for f in findings}
    surfaced = []
    for f in findings:
        row = conn.execute("SELECT * FROM alerts WHERE key = ?", (f["key"],)).fetchone()
        if row is None:
            conn.execute("INSERT INTO alerts (key, coin, criterion, severity, message, first_ts, surfaced_ts) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (f["key"], f["coin"], f["criterion"], f["severity"], f["message"], _iso(now), _iso(now)))
            surfaced.append(f["key"])
            continue
        reopened = row["closed"] == 1
        rose = RANK[f["severity"]] > RANK[row["severity"]]
        stale = (not row["seen"] and f["severity"] != INFO and now - _parse(row["surfaced_ts"]) >= REPEAT)
        if reopened or rose:
            conn.execute("UPDATE alerts SET severity=?, message=?, seen=0, closed=0, surfaced_ts=? WHERE key=?",
                         (f["severity"], f["message"], _iso(now), f["key"]))
            surfaced.append(f["key"])
        elif stale:
            conn.execute("UPDATE alerts SET message=?, surfaced_ts=? WHERE key=?",
                         (f["message"], _iso(now), f["key"]))
            surfaced.append(f["key"])
        else:
            conn.execute("UPDATE alerts SET message=?, severity=? WHERE key=?",
                         (f["message"], f["severity"], f["key"]))
    # a condição desapareceu: o alerta fecha sozinho
    for row in conn.execute("SELECT key FROM alerts WHERE closed = 0 AND key != ? AND key NOT LIKE 'bot:%'", (CONNECTION_KEY,)).fetchall():
        if row["key"] not in current:
            conn.execute("UPDATE alerts SET closed = 1 WHERE key = ?", (row["key"],))
    conn.commit()
    return surfaced


def connection_failed(conn, reason, now=None):
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT closed FROM alerts WHERE key = ?", (CONNECTION_KEY,)).fetchone()
    msg = f"Ligação à Binance falhou: {reason} As análises de risco ficam suspensas até a ligação voltar."
    if row is None:
        conn.execute("INSERT INTO alerts (key, coin, criterion, severity, message, first_ts, surfaced_ts) "
                     "VALUES (?, '-', 'ligacao', ?, ?, ?, ?)", (CONNECTION_KEY, HIGH, msg, _iso(now), _iso(now)))
    elif row["closed"]:
        conn.execute("UPDATE alerts SET message=?, seen=0, closed=0, surfaced_ts=? WHERE key=?",
                     (msg, _iso(now), CONNECTION_KEY))
    else:
        conn.execute("UPDATE alerts SET message=? WHERE key=?", (msg, CONNECTION_KEY))
    conn.commit()


def connection_ok(conn):
    conn.execute("UPDATE alerts SET closed = 1 WHERE key = ?", (CONNECTION_KEY,))
    conn.commit()


def mark_seen(conn, key):
    conn.execute("UPDATE alerts SET seen = 1 WHERE key = ?", (key,))
    conn.commit()


def open_alerts(conn):
    return conn.execute(
        "SELECT * FROM alerts WHERE closed = 0 ORDER BY "
        "CASE severity WHEN 'alto' THEN 0 WHEN 'atenção' THEN 1 ELSE 2 END, first_ts DESC").fetchall()


def unseen_top(conn):
    """Gravidade mais alta entre os alertas abertos por ver (para o ponto de cor do menu)."""
    best = None
    for r in conn.execute("SELECT severity FROM alerts WHERE closed = 0 AND seen = 0").fetchall():
        if best is None or RANK[r["severity"]] > RANK[best]:
            best = r["severity"]
    return best
