"""Alertas dos bots: aparecem sempre no painel e, se estiverem configurados, também no Telegram e no WhatsApp.

Os canais só ENVIAM mensagens (sem receber comandos): não abrem nenhuma porta de controlo dos bots. As credenciais ficam
em ficheiros protegidos fora da base de dados e do git (keystore.telegram_path() e keystore.whatsapp_path()). Na
Configuração > Alertas escolhes, por canal, se está ligado e a partir de que gravidade envia. Um alerta que falha nunca
pára um bot: o painel regista-o sempre.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import alerts, keystore
from .log import log

TELEGRAM_FILE = None            # os testes apontam isto para uma pasta temporária
WHATSAPP_FILE = None
SENDER = None                   # Telegram: os testes trocam por um gravador; None = envio real
WA_SENDER = None                # WhatsApp: idem
KEEP_DAYS = 7

# tipo de evento do motor -> gravidade do alerta
SEVERITY = {"stop": "alto", "reset": "alto", "guard": "alto", "error": "alto", "pause": "atenção",
            "blocked": "atenção"}
LEVEL = {"info": 1, "atenção": 2, "alto": 3}
MIN_CHOICES = (("atenção", "Atenção e alto (tudo o que importa)"), ("alto", "Só alto (o que exige ação)"))
DEFAULT_PREFS = {"alert_telegram_on": "1", "alert_telegram_min": "atenção",
                 "alert_whatsapp_on": "1", "alert_whatsapp_min": "alto"}

PROVIDERS = {"callmebot": "CallMeBot (grátis, para uso pessoal)", "cloud": "WhatsApp Cloud API (oficial da Meta)"}
GRAPH_VERSION = "v21.0"


def _path():
    return TELEGRAM_FILE or keystore.telegram_path()


def _wa_path():
    return WHATSAPP_FILE or keystore.whatsapp_path()


# ---------- Telegram ----------
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


# ---------- WhatsApp ----------
def normalize_phone(raw):
    """Só dígitos, em formato internacional sem o zero inicial (ex.: 351912345678). None se não parecer um número."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    return digits if 8 <= len(digits) <= 15 else None


def validate_whatsapp(form):
    """Valida os campos do formulário. Devolve (configuração limpa, erros em português simples)."""
    provider = form.get("provider", "")
    errors, cfg = [], {"provider": provider}
    phone = normalize_phone(form.get("phone", ""))
    if provider not in PROVIDERS:
        return None, ["Escolhe o serviço de WhatsApp."]
    if not phone:
        errors.append("O número tem de estar em formato internacional, com indicativo (ex.: +351 912 345 678).")
    cfg["phone"] = phone
    if provider == "callmebot":
        cfg["apikey"] = form.get("apikey", "").strip()
        if not cfg["apikey"]:
            errors.append("Falta a chave (apikey) que o CallMeBot te enviou por WhatsApp.")
    else:
        cfg["phone_number_id"] = re.sub(r"\D", "", form.get("phone_number_id", ""))
        cfg["token"] = form.get("token", "").strip()
        cfg["template"] = form.get("template", "").strip()
        if not cfg["phone_number_id"]:
            errors.append("Falta o identificador do número (Phone number ID) do painel da Meta.")
        if not cfg["token"]:
            errors.append("Falta o token de acesso da Meta.")
        if cfg["template"] and not re.fullmatch(r"[a-z0-9_]{1,512}", cfg["template"]):
            errors.append("O nome do modelo só pode ter letras minúsculas, números e _ (como está na Meta).")
    return (None, errors) if errors else (cfg, [])


def whatsapp_send(cfg, text, opener=urllib.request.urlopen):
    """Envia por WhatsApp. Devolve (ok, erro). Nunca levanta exceção e nunca inclui chaves ou tokens no erro."""
    try:
        if cfg["provider"] == "callmebot":
            query = urllib.parse.urlencode({"phone": "+" + cfg["phone"], "text": text[:900], "apikey": cfg["apikey"]})
            with opener(urllib.request.Request("https://api.callmebot.com/whatsapp.php?" + query), timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
                status = getattr(resp, "status", 200)
            if status == 200 and not re.search(r"error|invalid|not activated|blocked", body, re.I):
                return True, ""
            return False, "o CallMeBot recusou (confirma o número e a chave, e se ativaste o serviço)"
        url = f"https://graph.facebook.com/{GRAPH_VERSION}/{cfg['phone_number_id']}/messages"
        if cfg.get("template"):                     # fora das 24 h só são permitidos modelos aprovados
            payload = {"messaging_product": "whatsapp", "to": cfg["phone"], "type": "template",
                       "template": {"name": cfg["template"], "language": {"code": "pt_PT"},
                                    "components": [{"type": "body", "parameters": [{"type": "text",
                                                                                     "text": text[:900]}]}]}}
        else:
            payload = {"messaging_product": "whatsapp", "to": cfg["phone"], "type": "text",
                       "text": {"body": text[:900]}}
        request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Authorization": f"Bearer {cfg['token']}",
                                                  "Content-Type": "application/json"})
        with opener(request, timeout=10) as resp:
            body = json.load(resp)
        return ("messages" in body), "" if "messages" in body else "a Meta não aceitou a mensagem"
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode()).get("error", {})
            return False, f"a Meta recusou (código {err.get('code', exc.code)}): {str(err.get('message', ''))[:120]}"
        except Exception:
            return False, f"o serviço recusou o pedido ({exc.code})"
    except Exception:
        return False, "sem ligação ao serviço de WhatsApp"


# ---------- preferências e envio por todos os canais ----------
def prefs(conn):
    """Preferências dos canais (base de dados; por defeito: Telegram desde 'atenção', WhatsApp só 'alto')."""
    out = dict(DEFAULT_PREFS)
    for key in out:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row:
            out[key] = row[0]
    return out


def broadcast(conn, text, severity="alto"):
    """Envia para os canais configurados e ligados cuja gravidade mínima o alerta atinge. Devolve [(canal, ok, erro)]."""
    p = prefs(conn)
    level = LEVEL.get(severity, 3)
    results = []
    tg = keystore.load(_path())
    if tg and p["alert_telegram_on"] == "1" and level >= LEVEL.get(p["alert_telegram_min"], 2):
        ok, err = (SENDER or send)(tg[0], tg[1], text)
        results.append(("telegram", ok, err))
    wa = keystore.load_json(_wa_path())
    if wa and p["alert_whatsapp_on"] == "1" and level >= LEVEL.get(p["alert_whatsapp_min"], 3):
        ok, err = (WA_SENDER or whatsapp_send)(wa, text)
        results.append(("whatsapp", ok, err))
    for channel, ok, err in results:
        if not ok:
            log.warning("Alerta por %s não enviado: %s", channel, err)
    return results


def bot_events(conn, eng, events, now_ms=None):
    """Regista no painel (e envia para os canais ligados) os eventos que merecem atenção."""
    important = [e for e in events if e["kind"] in SEVERITY]
    if not important:
        return
    alerts.init(conn)
    now = datetime.now(timezone.utc)
    for i, e in enumerate(important):
        message = f"Bot {eng.id} {eng.pair} ({'Testnet' if eng.mode == 'testnet' else 'simulação'}): {e['detail']}"
        key = f"bot:{eng.id}:{e['ts']}:{e['kind']}:{i}"
        conn.execute("INSERT OR IGNORE INTO alerts (key, coin, criterion, severity, message, first_ts, surfaced_ts) "
                     "VALUES (?, ?, 'bot', ?, ?, ?, ?)",
                     (key, eng.pair, SEVERITY[e["kind"]], message,
                      now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")))
        broadcast(conn, f"Malha · {message}", SEVERITY[e["kind"]])
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    conn.execute("UPDATE alerts SET closed = 1 WHERE key LIKE 'bot:%' AND first_ts < ?", (cutoff,))
    conn.commit()
