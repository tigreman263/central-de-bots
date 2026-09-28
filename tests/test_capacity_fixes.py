"""Correções do diagnóstico de capacidade (docs/CAPACIDADE.md): RAM por processo, backoff de erros internos, e o
vigilante do PARAR TUDO não constrói o Trader sem necessidade. Nenhuma altera a lógica de trading."""
import platform
import time

import pytest

from app import botstore, capacity, db, notify, runner
from app.capacity import ATENCAO, CRITICO, ELEVADO, NORMAL, Monitor, evaluate
from app.engine import PAUSED, PENDING, RUNNING, STOPPED
from app.trader import TraderError
from test_capacity import metrics, sys_sample  # noqa: F401 (reaproveita os valores por defeito "confortáveis")
from test_grid import flat
from test_grid import candles as gcandles
from test_testnet import PAIR, T0, FakeExchange, candles, engine, tick, world  # noqa: F401


# ---------- 1. RAM: sistema vs. central ----------
def test_high_ram_from_other_programs_does_not_raise_the_state_when_the_app_itself_is_confirmed_healthy():
    """O caso real do diagnóstico: RAM do sistema a 93%, mas a app com 45 MB. Antes isto dava ATENÇÃO; agora não."""
    healthy_app = evaluate(metrics(ram=93.0, ram_app_mb=45.0))
    assert healthy_app["state"] == NORMAL
    unknown_app = evaluate(metrics(ram=93.0))                       # sem medição do processo: comportamento antigo
    assert unknown_app["state"] == ATENCAO


def test_ram_state_is_unchanged_when_process_measurement_is_unavailable_fallback():
    """Sem `ram_app_mb` (fallback documentado) o resultado é IGUAL ao de antes desta correção, em todos os níveis."""
    for ram in (70.0, 88.0, 90.0, 94.0, 96.0, 98.0):
        assert evaluate(metrics(ram=ram)) == evaluate(metrics(ram=ram, ram_app_mb=None))


def test_the_app_itself_using_a_lot_of_ram_is_a_real_signal_not_suppressed():
    """Se for a PRÓPRIA app a crescer (não outro programa), o aviso continua a aparecer — isto não escondeu nada."""
    r = evaluate(metrics(ram=93.0, ram_app_mb=350.0))                # central a usar 350 MB: real, fica visível
    assert r["state"] in (ATENCAO, ELEVADO)
    assert any(i["key"] == "ram_app" and i["level"] >= 1 for i in r["indicators"])


def test_app_ram_alone_can_still_reach_elevated_if_it_keeps_growing():
    r = evaluate(metrics(ram=50.0, ram_app_mb=600.0))                # RAM do sistema tranquila, mas a app cresceu muito
    assert any(i["key"] == "ram_app" and i["level"] == 3 for i in r["indicators"])
    assert r["state"] in (ELEVADO, CRITICO)


def test_system_ram_still_escalates_together_with_a_real_system_symptom():
    """Com o runner já atrasado, a RAM do sistema continua a contar (mesmo com a app "saudável" nesse instante)."""
    r = evaluate(metrics(ram=93.0, ram_app_mb=45.0, dur_recent=75.0))
    assert r["state"] in (ELEVADO, CRITICO)


def test_ram_display_shows_both_numbers_in_the_same_existing_row_no_new_ui_row():
    r = evaluate(metrics(ram=86.0, ram_app_mb=45.0))
    assert [d["label"] for d in r["display"]] == ["CPU", "RAM", "Disco", "Runner", "Rede", "Fila"]   # ainda 6 linhas
    assert "86" in r["display"][1]["text"] and "45" in r["display"][1]["text"]


def test_process_ram_reader_returns_a_plausible_number_on_this_machine_or_none():
    mb = capacity._own_process_ram_mb()
    assert mb is None or 1.0 < mb < 5000.0                            # nunca inventa um valor absurdo


@pytest.mark.skipif(platform.system() not in ("Windows", "Linux"), reason="leitura por processo só em Windows/Linux")
def test_process_ram_reader_works_on_this_real_machine():
    mb = capacity._own_process_ram_mb()
    assert mb is not None and mb > 1.0                                 # o próprio processo de teste já usa mais do que isto


def test_process_ram_flows_from_sampler_through_monitor_into_the_snapshot(tmp_path):
    from test_capacity import Script
    conn = db.connect(str(tmp_path / "c.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    mon = Monitor(sampler=Script(ram=93.0))
    mon.sampler.read = lambda: {**sys_sample(ram=93.0), "app_ram_mb": 45.0}
    capacity.after_cycle(conn, mon, time.time() - 1, time.time(), 60.0, {"bots": 0, "errors": 0})
    snap = capacity.load_snapshot(conn)
    assert snap["state"] == "normal"                                   # RAM alta, app saudável, medida ponta a ponta
    assert "central" in snap["display"][1]["text"]


def test_unknown_operating_system_falls_back_to_none_without_raising(monkeypatch):
    monkeypatch.setattr(capacity.platform, "system", lambda: "Plan9")
    assert capacity._own_process_ram_mb() is None


# ---------- 2. Backoff de erros internos ----------
def crash_engine(monkeypatch, target, exc_factory):
    def boom(*a, **k):
        raise exc_factory()
    monkeypatch.setattr(target[0], target[1], boom)


def test_first_internal_error_is_reported_immediately(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    monkeypatch.setattr(runner, "_advance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    tick(conn, ex, bid, 2)
    eng = engine(conn, bid)
    errs = [e for e in botstore.events(conn, bid, 50) if e["kind"] == "error"]
    assert len(errs) == 1 and "bug" not in errs[0]["detail"] and "RuntimeError" in errs[0]["detail"]
    assert eng.s["err_backoff"]["count"] == 1


def test_the_same_repeated_error_is_throttled_but_the_bot_keeps_retrying_every_cycle(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    monkeypatch.setattr(runner, "_advance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    for k in range(2, 10):                                              # 8 ciclos seguidos com o MESMO erro
        tick(conn, ex, bid, k)
    errs = [e for e in botstore.events(conn, bid, 50) if e["kind"] == "error"]
    assert 1 <= len(errs) < 8                                           # não 1 alerta por ciclo, mas também não 0
    eng = engine(conn, bid)
    assert eng.s["err_backoff"]["count"] == 8                           # continuou a tentar (e a contar) todos os ciclos


def test_a_bot_that_cannot_build_the_grid_for_lack_of_capital_does_not_spam_forever():
    """O caso concreto do diagnóstico: capital insuficiente para o par -> GridRefused a cada ciclo, sem fim."""
    conn = db.connect("file::memory:?cache=shared", uri=True) if False else None
    import tempfile
    from pathlib import Path
    d = tempfile.mkdtemp()
    conn = db.connect(str(Path(d) / "c.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    ex = FakeExchange(candles(flat(30)))
    ex.rules["min_notional"] = 500.0                                    # torna o par caro demais para o capital
    bid = botstore.create(conn, PAIR, 20.0, {**ex.rules}, {}, mode="testnet")
    for k in range(1, 15):                                              # 14 ciclos seguidos a falhar
        ex.now = T0 + k * 60_000
        runner.tick_bot(conn, ex, bid, ex.now, trader=ex)
    eng = engine(conn, bid)
    assert eng.status == PENDING                                       # continua a tentar (não fica preso num status errado)
    errs = [e for e in botstore.events(conn, bid, 50) if e["kind"] == "error"]
    assert 1 <= len(errs) <= 6                                          # muito menos do que 14 (um por ciclo seria o bug)
    assert "vai tentar outra vez" in errs[0]["detail"]                  # e o texto não inventa uma pausa que não houve


def test_a_different_error_type_is_not_blocked_by_a_previous_backoff(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    monkeypatch.setattr(runner, "_advance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("A")))
    for k in (2, 3, 4):
        tick(conn, ex, bid, k)
    n_a = len([e for e in botstore.events(conn, bid, 50) if e["kind"] == "error"])
    monkeypatch.setattr(runner, "_advance", lambda *a, **k: (_ for _ in ()).throw(ValueError("B")))    # erro DIFERENTE
    tick(conn, ex, bid, 5)
    errs = [e for e in botstore.events(conn, bid, 50) if e["kind"] == "error"]
    assert len(errs) == n_a + 1                                         # o novo tipo não ficou à espera do backoff do outro
    assert "ValueError" in errs[0]["detail"]


def test_recovering_clears_the_backoff_and_the_next_new_error_is_immediate_again(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    with monkeypatch.context() as m:
        m.setattr(runner, "_advance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
        for k in (2, 3, 4):
            tick(conn, ex, bid, k)
    assert engine(conn, bid).s.get("err_backoff")
    tick(conn, ex, bid, 5)                                              # sem falha: recuperou
    eng = engine(conn, bid)
    assert "err_backoff" not in eng.s
    with monkeypatch.context() as m:
        m.setattr(runner, "_advance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug de novo")))
        tick(conn, ex, bid, 6)
    errs = [e for e in botstore.events(conn, bid, 50) if e["kind"] == "error"]
    assert errs[0]["ts"] == T0 + 6 * 60_000                              # o novo erro (mesmo que o mesmo tipo) é imediato


def test_backoff_never_creates_extra_orders(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    placed_before = dict(ex.placed)
    monkeypatch.setattr(runner, "_advance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    for k in range(2, 8):
        tick(conn, ex, bid, k)
    assert ex.placed == placed_before                                   # nenhuma ordem nova só por causa dos erros/backoff


def test_emergency_stop_and_reconciliation_are_not_blocked_by_a_pending_backoff(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    with monkeypatch.context() as m:
        m.setattr(runner, "_advance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
        for k in (2, 3, 4):
            tick(conn, ex, bid, k)                                      # fica em backoff (o erro é no _advance, não no reconcile)
    assert engine(conn, bid).s.get("err_backoff")
    db.set_many(conn, {"emergency_stop": "1"})
    ex.now = T0 + 5 * 60_000
    runner.emergency_sweep(conn, ex, ex.now)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and not ex.open_cids()                 # o PARAR TUDO funcionou apesar do backoff pendente


def test_flush_internal_error_also_backs_off_and_clears_on_success(world, monkeypatch):
    """A ordem inicial fica 'sending' logo no 1.º passo: é aí que `_flush` corre (por isso o bug tem de estar já
    montado antes desse 1.º `tick`, não depois)."""
    conn, ex, bid = world

    class Boom:
        def flush(self, eng, now_ms):
            raise RuntimeError("bug ao enviar")

    real_flush = runner._flush

    def patched(conn_, eng, ex_, now_ms, warning):
        return real_flush(conn_, eng, Boom(), now_ms, warning)

    with monkeypatch.context() as m:
        m.setattr(runner, "_flush", patched)
        tick(conn, ex, bid, 1)
    eng = engine(conn, bid)
    assert eng.status == PAUSED and eng.s.get("err_backoff")
    errs = [e for e in botstore.events(conn, bid, 50) if e["kind"] == "error"]
    assert len(errs) == 1 and "erro interno" in errs[0]["detail"].lower()
    assert not ex.placed                                                # nunca chegou a sair nada (o envio falhou sempre)
    tick(conn, ex, bid, 2)                                               # sem o bug: envia normalmente e limpa o backoff
    eng = engine(conn, bid)
    assert "err_backoff" not in eng.s and ex.placed


# ---------- 3. Vigilante do PARAR TUDO ----------
def test_watcher_never_builds_a_trader_or_reads_keys_when_the_flag_is_off(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    conn.close()
    calls = []

    def factory():
        calls.append(1)
        raise AssertionError("não devia construir o Trader com a flag desligada")

    import threading
    t = threading.Thread(target=runner.watch, args=(str(tmp_path / "c.db"), factory, 0.05), daemon=True)
    t.start()
    time.sleep(0.3)
    assert calls == []                                                  # nunca chamado: a flag nunca esteve ligada


def test_watcher_builds_the_trader_and_sweeps_once_the_flag_turns_on(tmp_path, world):
    conn, ex, bid = world
    path = str(tmp_path / "app.db")
    conn2 = db.connect(path)
    conn2.executescript(db.SCHEMA)
    botstore.init(conn2)
    real_bid = botstore.create(conn2, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    ex.now = T0 + 60_000
    runner.tick_bot(conn2, ex, real_bid, ex.now, trader=ex)
    conn2.close()
    calls = []

    def factory():
        calls.append(1)
        return ex
    db.set_many(db.connect(path), {"emergency_stop": "1"})
    import threading
    t = threading.Thread(target=runner.watch, args=(path, factory, 0.05), daemon=True)
    t.start()
    time.sleep(0.4)
    assert calls                                                        # com a flag ligada, o Trader É construído
    assert botstore.get(db.connect(path), real_bid)["status"] in ("stopped", "stopping")


def test_watcher_still_reopens_a_connection_each_round_backup_restore_stays_safe(tmp_path):
    """Documenta a decisão: uma ligação nova a cada volta continua a ver um ficheiro restaurado por um backup."""
    import shutil
    path = str(tmp_path / "app.db")
    conn = db.connect(path)
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    bak = str(tmp_path / "antiga.db")
    import sqlite3
    out = sqlite3.connect(bak)
    conn.backup(out)
    out.close()
    db.set_many(conn, {"emergency_stop": "1"})                          # muda depois da cópia
    conn.close()
    for side in ("-wal", "-shm"):
        p = path + side
        import os
        if os.path.exists(p):
            os.remove(p)
    shutil.copy(bak, path)                                              # restauro: a flag volta a "0" (era o valor da cópia)
    assert db.get(db.connect(path), "emergency_stop") != "1"            # uma leitura NOVA já vê o ficheiro restaurado


def test_emergency_sweep_still_works_exactly_as_before_when_the_flag_is_on(world):
    """A otimização só evita construir o Trader com a flag desligada; ligada, o comportamento é idêntico ao de antes."""
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    db.set_many(conn, {"emergency_stop": "1"})
    ex.now = T0 + 2 * 60_000
    runner.emergency_sweep(conn, ex, ex.now)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and not ex.open_cids()


def test_watcher_survives_a_broken_trader_factory_and_keeps_polling(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    db.set_many(conn, {"emergency_stop": "1"})
    conn.close()
    calls = []

    def factory():
        calls.append(1)
        raise TraderError("Sem ligação à Testnet.")

    import threading
    t = threading.Thread(target=runner.watch, args=(str(tmp_path / "c.db"), factory, 0.05), daemon=True)
    t.start()
    time.sleep(0.3)
    assert len(calls) >= 2                                              # continuou a tentar apesar de a chamada falhar


def test_wal_mode_and_concurrent_reads_still_work_after_the_watcher_change(tmp_path, world):
    conn, ex, bid = world
    path = str(tmp_path / "shared.db")
    db.init_db(path)
    c1 = db.connect(path)
    botstore.init(c1)
    assert c1.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    c2 = db.connect(path)                                               # leitor concorrente: WAL não bloqueia
    assert tuple(c2.execute("select 1").fetchone()) == (1,)
    c1.close()
    c2.close()
