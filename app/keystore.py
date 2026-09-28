"""Guarda a chave da Binance num ficheiro protegido, fora da base de dados, do git e da pasta do projeto."""
import json
import os
import subprocess
from pathlib import Path

from . import config
from .log import log


def default_path():
    return config.keys_dir() / "binance_readonly.json"


def testnet_path():
    """Chaves da Binance TESTNET: ficheiro próprio, nunca misturado com a chave real de leitura."""
    return config.keys_dir() / "binance_testnet.json"


def real_trading_path():
    """Chave da conta REAL com permissão de negociar (nunca de levantar): ficheiro próprio, nunca misturado
    com a chave só de leitura do Portefólio. Guardar aqui não liga nada sozinho — isso exige ativação à parte."""
    return config.keys_dir() / "binance_real_trading.json"


def telegram_path():
    """Token do bot do Telegram (chave = token, segredo = id da conversa). Só serve para enviar mensagens."""
    return config.keys_dir() / "telegram.json"


def whatsapp_path():
    """Ligação do WhatsApp (CallMeBot ou Cloud API da Meta). Só serve para enviar alertas."""
    return config.keys_dir() / "whatsapp.json"


def protect(path):
    """Deixa o ficheiro (e a pasta) só para o utilizador atual. Devolve False (e avisa) se não conseguiu."""
    path = Path(path)
    ok = True
    try:
        os.chmod(path, 0o600)
        os.chmod(path.parent, 0o700)
    except OSError:
        ok = os.name == "nt"                # no Windows o chmod quase não faz nada: o que conta é o icacls
    if os.name == "nt":  # no Windows, tirar as permissões herdadas e deixar só o utilizador atual
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        who = f"{domain}\\{user}" if user and domain else user
        ok = False
        if who:
            ok = True
            for target, grant in ((path, f"{who}:F"), (path.parent, f"{who}:(OI)(CI)F")):   # a pasta passa-o aos ficheiros novos
                done = subprocess.run(["icacls", str(target), "/inheritance:r", "/grant:r", grant],
                                      capture_output=True, check=False)
                ok = ok and done.returncode == 0
    if not ok:
        log.warning("Não consegui restringir as permissões de %s: confirma-as à mão.", path.name)
    return ok


_protect = protect   # nome antigo


def write_private(path, text):
    """Escreve um ficheiro que nunca fica legível por outros, nem por um instante (cria-o já com 0600)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    protect(path)


def save(path, key, secret):
    write_private(path, json.dumps({"key": key.strip(), "secret": secret.strip()}))


def save_json(path, data):
    write_private(path, json.dumps(data))


def load_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def load(path):
    try:
        data = json.loads(Path(path).read_text())
        return data["key"], data["secret"]
    except (OSError, ValueError, KeyError):
        return None


def delete(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def masked(key):
    return f"…{key[-4:]}" if key and len(key) >= 4 else "…"
