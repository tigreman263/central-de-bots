"""Nenhum teste pode ler as chaves reais do utilizador, enviar mensagens reais para o Telegram nem falar com a Binance."""
import urllib.request
from pathlib import Path

import pytest

from app import keystore, notify


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Chaves em pasta temporária, Telegram gravado (não enviado) e rede bloqueada por defeito."""
    sent = []
    monkeypatch.setattr(notify, "TELEGRAM_FILE", tmp_path / "no-telegram.json")   # não existe: sem Telegram
    monkeypatch.setattr(notify, "SENDER", lambda token, chat, text: sent.append(text) or (True, ""))
    monkeypatch.setattr(notify, "WHATSAPP_FILE", tmp_path / "no-whatsapp.json")      # não existe: sem WhatsApp
    monkeypatch.setattr(notify, "WA_SENDER", lambda cfg, text: sent.append("[whatsapp] " + text) or (True, ""))
    home = tmp_path / "home"
    monkeypatch.setattr(keystore, "default_path", lambda: home / ".central-de-bots" / "binance_readonly.json")
    monkeypatch.setattr(keystore, "testnet_path", lambda: home / ".central-de-bots" / "binance_testnet.json")
    monkeypatch.setattr(keystore, "telegram_path", lambda: home / ".central-de-bots" / "telegram.json")
    monkeypatch.setattr(keystore, "whatsapp_path", lambda: home / ".central-de-bots" / "whatsapp.json")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def no_network(*args, **kwargs):
        raise AssertionError("Um teste tentou falar com a rede a sério (use uma exchange/leitor falso).")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    return sent


# os testes existentes usam este nome
isolated_notifications = isolated_environment
