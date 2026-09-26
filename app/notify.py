"""Alertas dos bots: aparecem sempre no painel e, se estiver configurado, também no Telegram.

O Telegram só ENVIA mensagens (sem receber comandos): não abre nenhuma porta de controlo dos bots.
O token e o id da conversa ficam num ficheiro protegido fora da base de dados e do git (keystore.telegram_path()).
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import alerts, keystore

TELEGRAM_FILE = None            # os testes apontam isto para uma pasta temporária
SENDER = None                   # os testes trocam por um gravador; None = envio real
KEEP_DAYS = 7

# tipo de evento do motor -> gravidade do alerta
SEVERITY = {"stop": "alto", "reset": "alto", "guard": "alto", "error": "alto", "pause": "atenção",
            "blocked": "atenção"}


def _path():
    return TELEGRAM_FILE or keystore.telegram_path()


def send(token, chat_id, text, opener=urllib.request.urlopen):
    """Envia uma mensagem. Devolve (ok, erro). Nunca levanta exceção: um alerta falhado não pode parar um bot."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        with opener(urllib.request.Request(url, data=data), timeout=8) as resp:
            body = json.load(resp)
        return bool(body.get("ok")), "" if body.get("ok") else "o Telegram recusou a mensagem"
    except Exception:
        return False, "sem ligação ao Telegram"   # nunca inclui o token na mensagem de erro


def bot_events(conn, eng, events, now_ms=None):
    """Regista no painel (e envia para o Telegram) os eventos que merecem atenção."""
    important = [e for e in events if e["kind"] in SEVERITY]
    if not important:
        return
    alerts.init(conn)
    now = datetime.now(timezone.utc)
    creds = keystore.load(_path())
    sender = SENDER or send
    for i, e in enumerate(important):
        message = f"Bot {eng.id} {eng.pair} ({'Testnet' if eng.mode == 'testnet' else 'simulação'}): {e['detail']}"
        key = f"bot:{eng.id}:{e['ts']}:{e['kind']}:{i}"
        conn.execute("INSERT OR IGNORE INTO alerts (key, coin, criterion, severity, message, first_ts, surfaced_ts) "
                     "VALUES (?, ?, 'bot', ?, ?, ?, ?)",
                     (key, eng.pair, SEVERITY[e["kind"]], message,
                      now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")))
        if creds:
            sender(creds[0], creds[1], f"Central de Bots · {message}")
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    conn.execute("UPDATE alerts SET closed = 1 WHERE key LIKE 'bot:%' AND first_ts < ?", (cutoff,))
    conn.commit()
