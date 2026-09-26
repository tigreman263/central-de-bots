"""Cópia de segurança da base de dados (segura com o corredor a escrever) e limpeza das cópias antigas.

Uso:  python -m app.backup --dest /mnt/usb/bots --keep-days 7
As chaves (Binance, Testnet, Telegram) NÃO entram aqui: copia-as à parte, cifradas (por exemplo com restic ou borg).
"""
import argparse
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import config


def backup(db_path, dest, keep_days=7, now=None):
    """Grava dest/app-AAAAMMDD-HHMM.db e apaga as cópias com mais de `keep_days`. Devolve o caminho da nova cópia."""
    now = now or time.time()
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"app-{datetime.fromtimestamp(now).strftime('%Y%m%d-%H%M')}.db"
    src = sqlite3.connect(str(db_path), timeout=30)
    out = sqlite3.connect(str(target))
    try:
        src.backup(out)                              # cópia consistente mesmo com escritas em curso
    finally:
        out.close()
        src.close()
    for old in dest.glob("app-*.db"):
        if old != target and now - old.stat().st_mtime > keep_days * 86400:
            old.unlink()
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(config.data_dir() / "app.db"))
    parser.add_argument("--dest", required=True)
    parser.add_argument("--keep-days", type=int, default=7)
    args = parser.parse_args()
    print(backup(args.db, args.dest, args.keep_days))


if __name__ == "__main__":
    main()
