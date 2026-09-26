"""Onde ficam os dados e as chaves. Por variáveis de ambiente, para separar dados de código (Pi, VMs, contentores).

BOTS_DATA_DIR  pasta da base de dados e da chave da sessão (por defeito: <projeto>/data)
BOTS_KEYS_DIR  pasta das chaves da Binance/Testnet e do Telegram (por defeito: ~/.central-de-bots)
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir():
    return Path(os.environ.get("BOTS_DATA_DIR") or ROOT / "data")


def keys_dir():
    return Path(os.environ.get("BOTS_KEYS_DIR") or Path.home() / ".central-de-bots")
