"""Corredor contínuo dos bots: processo separado do painel.

Bots em modo sim: simulação com dados públicos. Bots em modo testnet: ordens reais na Binance TESTNET (nunca na conta
real). Cada passo lê o estado da base de dados, aplica os comandos do painel, reconcilia com a exchange (só testnet),
processa as velas de 1 minuto em falta (também as perdidas durante um desligamento) e grava tudo numa só transação.
Só depois de gravar é que as ordens saem para a exchange (ver executor.py). Sem preços: não inventa nada, regista o
aviso e espera. Um bot com erro nunca derruba os outros, e os erros ficam no registo (log).

PARAR TUDO: cada bot da Testnet passa a STOPPING (nunca salta para STOPPED); `_stop_rounds` cancela, contabiliza, fecha a
posição e só dá o bot por parado quando a exchange o confirma. Sem ligação continua STOPPING e tenta outra vez. O prazo
(`emergency_deadline_s`, por defeito 120 s) não declara nada parado: só dispara um alerta CRÍTICO.
"""
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import botstore, capacity, db, notify
from .engine import MIN, PAUSED, PENDING, RECOVERING, RUNNING, STOPPED, STOPPING
from .executor import TestnetExecutor
from .log import log
from .reader import BinanceError
from .trader import FATAL_CODES, TraderError

MAX_PAGES = 6          # até ~100 h de velas em falta por passo
MAX_SEND_FAILURES = 5  # passos seguidos sem conseguir enviar ordens: pausa de segurança
MAX_STOP_ROUNDS = 6    # voltas de reconcile→cancelar→fechar por chamada (a seguir, o vigilante ou o passo seguinte continuam)
STOP_DEADLINE_S = 120  # prazo por defeito para a paragem ser confirmada; passado, alerta CRÍTICO (configurável)
ERR_BACKOFF_BASE_S = 60     # erro interno repetido (mesmo tipo, mesmo sítio): 1.º alerta imediato, o 2.º espera isto
ERR_BACKOFF_FACTOR = 4      # cada repetição multiplica a folga por isto
ERR_BACKOFF_MAX_S = 3600    # ...até no máximo 1 alerta por hora enquanto o mesmo erro persistir

# o passo principal (60 s) e o vigilante do PARAR TUDO (~5 s) correm em threads: nunca mexem no mesmo bot ao mesmo tempo.
# Um fecho por bot (não global): um bot lento a falar com a exchange não atrasa a paragem dos outros.
_BOT_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def bot_lock(bot_id):
    with _LOCKS_GUARD:
        return _BOT_LOCKS.setdefault(bot_id, threading.RLock())


def stop_deadline_ms(conn):
    """Prazo da paragem de emergência, em ms (definição `emergency_deadline_s`; 0 desliga o alerta)."""
    try:
        return max(0, int(float(db.get(conn, "emergency_deadline_s", STOP_DEADLINE_S)) * 1000))
    except (TypeError, ValueError):
        return STOP_DEADLINE_S * 1000


def _report_internal_error(eng, now_ms, exc, context, detail):
    """Erro interno (bug do próprio código, não a exchange): o 1.º é sempre visível de imediato.

    O MESMO erro (mesmo sítio + mesmo tipo) repetido em ciclos seguidos entra em backoff: o intervalo até ao próximo
    alerta cresce a cada repetição (60 s, 240 s, 960 s, ... até 1 h), para não inundar o painel/Telegram com o mesmo
    aviso a cada minuto (ex.: um bot que nunca consegue montar a grelha por falta de capital). Um erro DIFERENTE
    (sítio ou tipo diferentes) reinicia a folga: nunca fica escondido por um backoff de outro problema. Quando um
    passo corre bem outra vez, `eng.s["err_backoff"]` é apagado (ver os pontos onde isso acontece em `tick_bot`/
    `_flush`) e o próximo erro, seja qual for, volta a ser imediato. Nunca altera ordens nem estratégia: só decide
    se este evento fica visível já ou mais tarde.
    """
    kind = f"{context}:{type(exc).__name__}"
    bo = eng.s.get("err_backoff")
    if not bo or bo.get("kind") != kind:
        bo = {"kind": kind, "count": 0, "next_ts": 0, "delay_s": ERR_BACKOFF_BASE_S}
    bo["count"] += 1
    if now_ms >= bo["next_ts"]:
        suffix = f" (repetiu-se {bo['count']}x; o próximo aviso só sai dentro de {bo['delay_s']:g} s se continuar)" \
            if bo["count"] > 1 else ""
        eng._event(now_ms, "error", detail + suffix)
        bo["next_ts"] = now_ms + bo["delay_s"] * 1000
        bo["delay_s"] = min(bo["delay_s"] * ERR_BACKOFF_FACTOR, ERR_BACKOFF_MAX_S)
    eng.s["err_backoff"] = bo


def _closed(candles, now_ms):
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in candles
            if int(k[0]) + MIN <= now_ms]


def _stop_check(conn):
    """O pedido de paragem lido AGORA na base de dados (PARAR TUDO ou comando stop deste bot), não o do início do passo."""
    def check(eng):
        if db.get(conn, "emergency_stop") == "1":
            return "PARAR TUDO"
        row = botstore.get(conn, eng.id)
        return "pedido do utilizador" if row is not None and row["command"] == "stop" else None
    return check


def _executor(eng, trader, conn=None):
    if eng.mode != "testnet" or trader is None:
        return None
    return TestnetExecutor(trader, _stop_check(conn) if conn is not None else None)


def _exchange_failure(eng, exc, now_ms):
    """Erro da exchange: sem ligação espera; chaves inválidas põem o bot em pausa segura (e cancelam as compras)."""
    if exc.code in FATAL_CODES and eng.status == RUNNING:
        eng._pause(now_ms, f"Testnet: {exc}")
        eng._event(now_ms, "error", f"Bot em pausa: {exc}")
    return f"Sem resposta da Testnet ({exc}). O bot espera; nada é inventado."


def _apply_commands(eng, row, now_ms, stop_all):
    """Comandos do painel e PARAR TUDO. Só mexe no estado local (sem rede): por isso nunca se perdem por falha de rede."""
    close = eng.s.get("last_close")
    if row["command"] and not (row["command"] == "activate" and stop_all):     # com o PARAR TUDO ligado ninguém arranca
        eng.command(row["command"], now_ms, close)
    if stop_all and eng.status in (RUNNING, PAUSED, RECOVERING) and eng.grid:
        eng.command("stop", now_ms, close, reason="PARAR TUDO")   # testnet: STOPPING (a paragem inclui a recuperação)


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
    ex = _executor(eng, trader, conn)
    if eng.status == STOPPED and not (ex and eng.has_exchange_work()) and row["command"] != "activate":
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
            ex.reconcile(eng, now_ms, claimed=botstore.claims(conn, eng))
        if eng.status == PENDING and stop_all:
            eng.status, eng.reason = STOPPED, "PARAR TUDO"
        elif eng.status not in (STOPPED, RECOVERING, STOPPING):   # em recuperação/paragem as velas esperam: nada se cria
            _advance(eng, source, now_ms)
        eng.s.pop("err_backoff", None)      # o passo correu bem: uma falha futura, seja qual for, volta a ser imediata
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
            detail = f"Erro interno ({type(exc).__name__}): o bot ficou em pausa por segurança."
            warning = "Erro interno: o bot ficou em pausa por segurança."
        else:                                            # ex.: ainda PENDING (não há posição/ordens para pôr em pausa)
            detail = f"Erro interno ({type(exc).__name__}): o bot vai tentar outra vez no próximo passo."
            warning = "Erro interno: o bot vai tentar outra vez no próximo passo."
        _report_internal_error(eng, now_ms, exc, "tick", detail)
    events = list(eng.new_events)
    botstore.save_engine(conn, eng, warning)              # estado (com as ordens a enviar) gravado ANTES de enviar
    if row["command"] and applied:
        botstore.clear_command(conn, bot_id, expected=row["command"])   # só apaga o comando que foi mesmo aplicado
    if ex is not None and eng.has_exchange_work():
        events += _flush(conn, eng, ex, now_ms, warning)
    if ex is not None and eng.status == STOPPING and not warning and not eng.s.get("send_failures"):
        events += _stop_rounds(conn, eng, ex, now_ms)      # a 1.ª ronda correu bem: continua até confirmar (senão espera pelo próximo passo)
    elif eng.status == STOPPING:
        eng.check_stop_overdue(now_ms, stop_deadline_ms(conn))
        events += list(eng.new_events)
        botstore.save_engine(conn, eng, warning)
    if not defer_notify:
        notify.bot_events(conn, eng, events, now_ms)
    return eng, events


def _stop_signature(eng):
    st = eng.s.get("stop") or {}
    return (eng.status, len(eng.s.get("cancel_queue") or []), tuple(sorted((o.get("cid"), o.get("state")) for o in eng.orders)),
            round(eng.s.get("base") or 0.0, 9), st.get("attempts"))


def _stop_rounds(conn, eng, ex, now_ms, warning=""):
    """Empurra um bot em STOPPING até onde a exchange deixar: reconcile → gravar → cancelar/fechar → repetir.

    Sem ligação (ou com erro num cancelamento) o bot fica STOPPING, com o estado gravado, e tenta-se no passo seguinte.
    Nunca dá o bot por parado: quem o faz é o executor, depois de confirmar na exchange. Devolve os eventos para alertas.
    """
    events = []
    for _ in range(MAX_STOP_ROUNDS):
        before = _stop_signature(eng)
        try:
            ex.reconcile(eng, now_ms, claimed=botstore.claims(conn, eng))
        except TraderError as exc:
            warning = warning or _exchange_failure(eng, exc, now_ms)
            break
        except Exception as exc:                        # um erro interno nunca derruba os outros bots
            log.exception("bot %s: erro interno a reconciliar a paragem", eng.id)
            eng._event(now_ms, "error", f"Erro interno na paragem ({type(exc).__name__}): continua a tentar.")
            warning = warning or "Erro interno na paragem: continua a tentar."
            break
        events += list(eng.new_events)
        botstore.save_engine(conn, eng, warning)         # estado (com a ordem de fecho por enviar) gravado ANTES de enviar
        if eng.status != STOPPING:
            break
        failures = eng.s.get("send_failures", 0)
        events += _flush(conn, eng, ex, now_ms, warning)
        if eng.s.get("send_failures", 0) > failures or _stop_signature(eng) == before:
            break                                        # a exchange falhou, ou nada mudou: fica para o passo seguinte
    if eng.status == STOPPING:
        eng.check_stop_overdue(now_ms, stop_deadline_ms(conn))
    events += list(eng.new_events)
    botstore.save_engine(conn, eng, warning)
    return events


def _flush(conn, eng, ex, now_ms, warning):
    try:
        ex.flush(eng, now_ms)
        eng.s["send_failures"] = 0
        eng.s.pop("err_backoff", None)
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
        _report_internal_error(eng, now_ms, exc, "flush",
                               f"Erro interno ao enviar ordens ({type(exc).__name__}): pausa de segurança.")
        warning = warning or "Erro interno ao enviar ordens: o bot ficou em pausa por segurança."
    events = list(eng.new_events)
    botstore.save_engine(conn, eng, warning)
    return events


def _mark_stopping(conn, bot_id, now_ms):
    """1.ª fase do PARAR TUDO, só com a base de dados (sem rede): o bot passa a STOPPING (ou STOPPED se for simulação)."""
    eng = botstore.load_engine(conn, bot_id)
    if eng is None:
        return []
    if eng.status in (RUNNING, PAUSED, RECOVERING) and eng.grid:
        eng.command("stop", now_ms, eng.s.get("last_close"), reason="PARAR TUDO")
    elif eng.status == PENDING:
        eng.status, eng.reason = STOPPED, "PARAR TUDO"
    else:
        return []
    events = list(eng.new_events)
    botstore.save_engine(conn, eng, "")
    return events                                       # para os alertas (quem chama decide quando os envia)


def emergency_bot(conn, trader, bot_id, now_ms, defer_notify=False):
    """PARAR TUDO: cancela as ordens e fecha a posição deste bot, e só o dá por parado depois de a exchange o confirmar."""
    events = _mark_stopping(conn, bot_id, now_ms)
    eng = botstore.load_engine(conn, bot_id)
    if eng is None:
        return None
    ex = _executor(eng, trader, conn)
    if not (ex and (eng.status == STOPPING or eng.has_exchange_work())):
        if not events:
            return None
        if not defer_notify:
            notify.bot_events(conn, eng, events, now_ms)
        return eng, events
    if eng.status == STOPPING:
        events += _stop_rounds(conn, eng, ex, now_ms)
    else:
        events += _flush(conn, eng, ex, now_ms, "")
    if not defer_notify:
        notify.bot_events(conn, eng, events, now_ms)
    return eng, events


def _active(conn, row):
    """Bot que o corredor tem de tratar: a trabalhar, com trabalho pendente na exchange, ou parado com um ATIVAR à espera."""
    return row["status"] != STOPPED or botstore.has_pending(conn, row) or row["command"] == "activate"


def tick_all(conn, reader, now_ms=None, trader=None):
    """Um ciclo de todos os bots. Devolve {"bots": processados, "errors": passos falhados} (para o monitor de capacidade)."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    done = failed = 0
    for row in botstore.all_bots(conn):
        try:
            if _active(conn, row):
                with bot_lock(row["id"]):
                    result = tick_bot(conn, reader, row["id"], now_ms, trader, defer_notify=True)
                done += 1
                if result:                              # alertas fora do fecho (e, no corredor, em segundo plano)
                    notify.bot_events(conn, result[0], result[1], now_ms)
        except Exception:
            failed += 1
            log.exception("bot %s: passo falhado", row["id"])
    db.set_many(conn, {"runner_heartbeat": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"bots": done, "errors": failed}


def emergency_sweep(conn, trader, now_ms=None):
    """Se o PARAR TUDO está ativo: 1.ª passagem, todos os bots entram já em STOPPING (só base de dados); 2.ª, cada um
    é empurrado até à confirmação. Um bot com erro (ou lento) nunca impede os outros de entrarem em STOPPING."""
    if db.get(conn, "emergency_stop") != "1":
        return
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    rows = [r for r in botstore.all_bots(conn) if _active(conn, r)]
    early = {}
    for row in rows:
        try:
            with bot_lock(row["id"]):
                early[row["id"]] = _mark_stopping(conn, row["id"], now_ms)
        except Exception:
            log.exception("bot %s: falhou a marcar a paragem (vai tentar de novo)", row["id"])
    for row in rows:
        try:
            with bot_lock(row["id"]):
                result = emergency_bot(conn, trader, row["id"], now_ms, defer_notify=True)
            events = early.get(row["id"], []) + (result[1] if result else [])
            if events:
                notify.bot_events(conn, result[0] if result else botstore.load_engine(conn, row["id"]), events, now_ms)
        except Exception:
            log.exception("bot %s: falhou o PARAR TUDO (vai tentar de novo)", row["id"])


def loop(db_path, reader_factory, every=60, trader_factory=None):
    monitor = capacity.Monitor(data_dir=Path(db_path).parent)      # só mede e informa; nunca mexe nos bots
    while True:
        try:
            conn = db.connect(db_path)
            try:
                t0 = time.time()
                summary = tick_all(conn, reader_factory(), trader=trader_factory() if trader_factory else None)
                capacity.after_cycle(conn, monitor, t0, time.time(), every, summary)
            finally:
                conn.close()
        except Exception:
            log.exception("passo do corredor falhou; o próximo tenta de novo")
        time.sleep(every)


def watch(db_path, trader_factory=None, every=5):
    """Vigilante leve do PARAR TUDO: não espera pelo passo de 60 s. Nunca morre por causa de um erro.

    Uma nova ligação SQLite por volta (a cada ~5 s), de propósito: manter uma ligação aberta durante muito tempo numa
    thread à parte pouparia microssegundos, mas arriscaria não ver um ficheiro restaurado por um backup (a Fase 1
    documenta esse cenário: `-wal`/`-shm` removidos e a base de dados copiada por cima) enquanto a ligação antiga
    continuasse presa ao ficheiro antigo. Reabrir é barato (medido: não é o custo que preocupa) e sem esse risco.
    O que SE poupa aqui: com a flag desligada (o caso normal) nem se lê o ficheiro das chaves nem se constrói o
    `Trader` — só depois de confirmar que há mesmo um PARAR TUDO em curso é que isso acontece.
    """
    while True:
        try:
            conn = db.connect(db_path)
            try:
                if db.get(conn, "emergency_stop") == "1":
                    emergency_sweep(conn, trader_factory() if trader_factory else None)
            finally:
                conn.close()
        except Exception:
            log.exception("vigilante do PARAR TUDO falhou; tenta de novo")
        time.sleep(every)
