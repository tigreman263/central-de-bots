"""Corredor dos bots (processo separado do painel).

Bots em simulação usam dados públicos. Bots em modo Testnet enviam ordens SÓ para a Binance Testnet (dinheiro fictício),
com as chaves da Testnet guardadas na pasta de chaves (BOTS_KEYS_DIR, por defeito ~/.central-de-bots). A conta real
nunca é tocada. Um vigilante leve verifica o PARAR TUDO a cada ~5 s; o passo principal corre a cada 60 s.
Só corre uma instância de cada vez (ficheiro de bloqueio na pasta de dados).

Uso:  python run_bots.py
"""
import sys
import threading

from app import botstore, config, db, keystore, lock, notify, runner
from app.log import log, setup
from app.reader import BinanceReader
from app.trader import Trader, TraderError

DB_PATH = str(config.data_dir() / "app.db")


def make_trader():
    creds = keystore.load(keystore.testnet_path())
    return Trader(*creds) if creds else Trader()      # sem chaves: só dados públicos, nenhuma ordem


def check_clock():
    """O relógio do Pi (sem RTC) pode arrancar desajustado: a Binance recusa pedidos assinados com mais de alguns segundos."""
    try:
        import time
        drift = Trader().server_time() - int(time.time() * 1000)
    except TraderError:
        log.warning("Não consegui comparar o relógio com a Binance (sem ligação).")
        return
    if abs(drift) > 2000:
        log.warning("O relógio deste computador difere %.1f s da Binance. Ativa a sincronização de hora (NTP); "
                    "entretanto o corredor compensa a diferença.", drift / 1000)


if __name__ == "__main__":
    setup()
    config.data_dir().mkdir(parents=True, exist_ok=True)
    held = lock.acquire(config.data_dir() / "runner.lock")
    if held is None:
        print("Já há um corredor dos bots a correr. Só pode haver um (senão as ordens duplicavam).")
        sys.exit(1)
    db.init_db(DB_PATH)
    conn = db.connect(DB_PATH)
    botstore.init(conn)
    conn.close()
    check_clock()
    log.info("Corredor dos bots a correr (simulação e Testnet). Ctrl+C para parar.")
    notify.start_worker()                                  # alertas em segundo plano: nunca atrasam a paragem
    threading.Thread(target=runner.watch, args=(DB_PATH, make_trader, 5), daemon=True).start()
    runner.loop(DB_PATH, lambda: BinanceReader(), trader_factory=make_trader)
