"""Corredor contínuo dos bots: processo separado do painel.

Bots em modo sim: simulação com dados públicos. Bots em modo testnet: ordens reais na Binance TESTNET (nunca na conta
real). Cada passo lê o estado da base de dados, aplica os comandos do painel, reconcilia com a exchange (só testnet),
processa as velas de 1 minuto em falta (também as perdidas durante um desligamento) e grava tudo numa só transação.
Só depois de gravar é que as ordens saem para a exchange (ver executor.py). Sem preços: não inventa nada, regista o
aviso e espera. Um bot com erro nunca derruba os outros, e os erros ficam no registo (log).
"""
import threading
import time
from datetime import datetime, timezone

from . import botstore, db, notify
from .engine import MIN, PAUSED, PENDING, RUNNING, STOPPED
from .executor import TestnetExecutor
from .log import log
from .reader import BinanceError
from .trader import FATAL_CODES, TraderError

MAX_PAGES = 6          # até ~100 h de velas em falta por passo
MAX_SEND_FAILURES = 5  # passos seguidos sem conseguir enviar ordens: pausa de segurança

# o passo principal (60 s) e o vigilante do PARAR TUDO (~5 s) correm em threads: nunca mexem no mesmo bot ao mesmo tempo
RUN_LOCK = threading.RLock()


def _closed(candles, now_ms):
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in candles
            if int(k[0]) + MIN <= now_ms]


def _executor(eng, trader):
    return TestnetExecutor(trader) if eng.mode == "testnet" and trader is not None else None


def _exchange_failure(eng, exc, now_ms):
    """Erro da exchange: sem ligação espera; chaves inválidas põem o bot em pausa segura (e cancelam as compras)."""
    if exc.code in FATAL_CODES and eng.status == RUNNING:
        eng._pause(now_ms, f"Testnet: {exc}")
        eng._event(now_ms, "error", f"Bot em pausa: {exc}")
    return f"Sem resposta da Testnet ({exc}). O bot espera; nada é inventado."


def _apply_commands(eng, row, now_ms, stop_all):
    """Comandos do painel e PARAR TUDO. Só mexe no estado local (sem rede): por isso nunca se perdem por falha de rede."""
    close = eng.s.get("last_close")
    if row["command"]:
        eng.command(row["command"], now_ms, close)
    if stop_all and eng.status in (RUNNING, PAUSED) and eng.grid:
        eng.command("stop", now_ms, close)
        eng.reason = "PARAR TUDO"


def _advance(eng, source, now_ms):
    """Processa as velas em falta. `source` dá as velas (Binance real ou Testnet, conforme o bot)."""
    start = eng.last_ts + MIN if eng.last_ts else None
    pages = 0
    while eng.status != STOPPED and pages < MAX_PAGES:
        pages += 1
        if start is None:                                    # primeiro passo: só a última vela fechada
            fetched = _closed(source.klines_1m(eng.pair, limit=3), now_ms)[-1:]
        else:
            fetched = _closed(source.klines_1m(eng.pair, start_ms=start, limit=1000), now_ms)
        if not fetched:
            break
        for c in fetched:
            eng.process_candle(c)
        start = eng.last_ts + MIN
        if len(fetched) < 1000:
            break


def tick_bot(conn, reader, bot_id, now_ms, trader=None, defer_notify=False):
    """Um passo de um bot. Devolve (motor, eventos) para os alertas; com defer_notify=False envia-os já."""
    eng = botstore.load_engine(conn, bot_id)
    row = botstore.get(conn, bot_id)
    if eng is None:
        return None
    ex = _executor(eng, trader)
    if eng.status == STOPPED and not (ex and eng.has_exchange_work()):
        return None
    warning = ""
    stop_all = db.get(conn, "emergency_stop") == "1"
    source = trader if row["source"] == "testnet" and trader is not None else reader
    if eng.mode == "testnet" and ex is None:
        botstore.save_engine(conn, eng, "Faltam as chaves da Testnet: o bot espera.")
        return None
    applied = False
    try:
        _apply_commands(eng, row, now_ms, stop_all)
        applied = True
        if ex is not None:
            ex.reconcile(eng, now_ms)
        if eng.status == PENDING and stop_all:
            eng.status, eng.reason = STOPPED, "PARAR TUDO"
        elif eng.status != STOPPED:
            _advance(eng, source, now_ms)
    except BinanceError as exc:
        warning = f"Sem dados da Binance ({exc}). O bot espera; nada é inventado."
    except TraderError as exc:
        warning = _exchange_failure(eng, exc, now_ms)
    except Exception as exc:  # um bot com erro nunca derruba os outros
        log.exception("bot %s: erro interno no passo", bot_id)
        eng = botstore.load_engine(conn, bot_id)        # descarta o estado a meio (nada fica contado duas vezes)
        applied = False
        if eng.status == RUNNING:
            eng.status, eng.reason = PAUSED, f"erro interno: {type(exc).__name__}"
        eng._event(now_ms, "error", f"Erro interno ({type(exc).__name__}): o bot ficou em pausa por segurança.")
        warning = "Erro interno: o bot ficou em pausa por segurança."
    events = list(eng.new_events)
    botstore.save_engine(conn, eng, warning)              # estado (com as ordens a enviar) gravado ANTES de enviar
    if row["command"] and applied:
        botstore.clear_command(conn, bot_id, expected=row["command"])   # só apaga o comando que foi mesmo aplicado
    if ex is not None and eng.has_exchange_work():
        events += _flush(conn, eng, ex, now_ms, warning)
    if not defer_notify:
        notify.bot_events(conn, eng, events, now_ms)
    return eng, events


def _flush(conn, eng, ex, now_ms, warning):
    try:
        ex.flush(eng, now_ms)
        eng.s["send_failures"] = 0
    except TraderError as exc:
        warning = warning or _exchange_failure(eng, exc, now_ms)
        eng.s["send_failures"] = eng.s.get("send_failures", 0) + 1
        if eng.s["send_failures"] >= MAX_SEND_FAILURES and eng.status == RUNNING:
            eng._pause(now_ms, f"não consegui enviar ordens durante {MAX_SEND_FAILURES} passos seguidos")
            eng._event(now_ms, "error", "Sem conseguir enviar ordens à Testnet: bot em pausa de segurança.")
    except Exception as exc:
        log.exception("bot %s: erro interno ao enviar ordens", eng.id)
        if eng.status == RUNNING:
            eng.status, eng.reason = PAUSED, f"erro interno: {type(exc).__name__}"
        eng._event(now_ms, "error", f"Erro interno ao enviar ordens ({type(exc).__name__}): pausa de segurança.")
        warning = warning or "Erro interno ao enviar ordens: o bot ficou em pausa por segurança."
    events = list(eng.new_events)
    botstore.save_engine(conn, eng, warning)
    return events


def emergency_bot(conn, trader, bot_id, now_ms, defer_notify=False):
    """PARAR TUDO: fecha a posição e cancela as ordens deste bot. Corre no vigilante rápido (~5 s)."""
    eng = botstore.load_engine(conn, bot_id)
    if eng is None:
        return None
    ex = _executor(eng, trader)
    events = []
    if eng.status in (RUNNING, PAUSED) and eng.grid:
        eng.command("stop", now_ms, eng.s.get("last_close"))
        eng.reason = "PARAR TUDO"
    elif eng.status == PENDING:
        eng.status, eng.reason = STOPPED, "PARAR TUDO"
    elif not (ex and eng.has_exchange_work()):
        return None
    events += list(eng.new_events)
    botstore.save_engine(conn, eng, "")
    if ex is not None and eng.has_exchange_work():
        events += _flush(conn, eng, ex, now_ms, "")
    if not defer_notify:
        notify.bot_events(conn, eng, events, now_ms)
    return eng, events


def _active(conn, row):
    return row["status"] != STOPPED or botstore.has_pending(conn, row)


def tick_all(conn, reader, now_ms=None, trader=None):
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    for row in botstore.all_bots(conn):
        try:
            if _active(conn, row):
                with RUN_LOCK:
                    result = tick_bot(conn, reader, row["id"], now_ms, trader, defer_notify=True)
                if result:                              # Telegram fora do bloqueio: a rede lenta não atrasa o PARAR TUDO
                    notify.bot_events(conn, result[0], result[1], now_ms)
        except Exception:
            log.exception("bot %s: passo falhado", row["id"])
    db.set_many(conn, {"runner_heartbeat": datetime.now(timezone.utc).isoformat(timespec="seconds")})


def emergency_sweep(conn, trader, now_ms=None):
    """Se o PARAR TUDO está ativo, trata já os bots que ainda têm algo por fechar (um erro num bot não pára os outros)."""
    if db.get(conn, "emergency_stop") != "1":
        return
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    for row in botstore.all_bots(conn):
        try:
            if _active(conn, row):
                with RUN_LOCK:
                    result = emergency_bot(conn, trader, row["id"], now_ms, defer_notify=True)
                if result:
                    notify.bot_events(conn, result[0], result[1], now_ms)
        except Exception:
            log.exception("bot %s: falhou o PARAR TUDO (vai tentar de novo)", row["id"])


def loop(db_path, reader_factory, every=60, trader_factory=None):
    while True:
        try:
            conn = db.connect(db_path)
            try:
                tick_all(conn, reader_factory(), trader=trader_factory() if trader_factory else None)
            finally:
                conn.close()
        except Exception:
            log.exception("passo do corredor falhou; o próximo tenta de novo")
        time.sleep(every)


def watch(db_path, trader_factory=None, every=5):
    """Vigilante leve do PARAR TUDO: não espera pelo passo de 60 s. Nunca morre por causa de um erro."""
    while True:
        try:
            conn = db.connect(db_path)
            try:
                emergency_sweep(conn, trader_factory() if trader_factory else None)
            finally:
                conn.close()
        except Exception:
            log.exception("vigilante do PARAR TUDO falhou; tenta de novo")
        time.sleep(every)
