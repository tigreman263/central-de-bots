"""Vigia de fora: avisa no Telegram se o corredor deixar de dar sinal (um corredor morto não consegue avisar).

Corre de minuto a minuto por um temporizador do sistema (ver deploy/). Só envia mensagens.
Uso:  python -m app.watchdog
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import botstore, config, db, keystore, notify

MAX_AGE_S = 180          # sem sinal há mais de 3 minutos: corredor parado
REPEAT_S = 3600          # repete o aviso no máximo de hora a hora


def heartbeat_age(conn, now=None):
    beat = db.get(conn, "runner_heartbeat")
    if not beat:
        return None
    now = now or datetime.now(timezone.utc).timestamp()
    return now - datetime.fromisoformat(beat).timestamp()


def check(conn, state_path, now=None, sender=None):
    """Devolve 'parado', 'voltou' ou None. Só avisa quando há bots ativos à espera do corredor."""
    now = now or time.time()
    sender = sender or notify.SENDER or notify.send
    state_path = Path(state_path)
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    age = heartbeat_age(conn, now)
    active = any(r["status"] != "stopped" for r in botstore.all_bots(conn))
    creds = keystore.load(notify.TELEGRAM_FILE or keystore.telegram_path())
    result = None
    if active and (age is None or age > MAX_AGE_S):
        if now - state.get("alerted", 0) >= REPEAT_S:
            result = "parado"
            state["alerted"] = now
            if creds:
                sender(creds[0], creds[1], "Malha · o corredor dos bots parou de dar sinal"
                                           + (f" há {int(age // 60)} min." if age is not None else "."))
    elif state.get("alerted"):
        result = "voltou"
        state["alerted"] = 0
        if creds:
            sender(creds[0], creds[1], "Malha · o corredor voltou a dar sinal.")
    state_path.write_text(json.dumps(state))
    return result


def main():
    conn = db.connect(str(config.data_dir() / "app.db"))
    try:
        print(check(conn, config.data_dir() / "watchdog.json") or "ok")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
