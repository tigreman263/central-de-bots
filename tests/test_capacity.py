"""Monitor de capacidade operacional: avalia se o equipamento tem margem, sem números mágicos de bots.

Cobre a avaliação composta, a histerese (sem falsos alertas), os alertas por mudança de estado, o histórico leve, a recolha
em Windows/Linux, a simulação de 1..50 bots, a ausência de interferência com o runner/emergência/SQLite e o painel.
"""
import json
import platform
import sqlite3
import time
import urllib.error

import pytest

from app import botstore, capacity, db, keystore, netmeter, notify, runner
from app.capacity import ATENCAO, CRITICO, ELEVADO, NORMAL, STATES, Monitor, aggregate, evaluate
from app.trader import Trader, TraderError
from test_recovery import db_path
from test_testnet import PAIR, T0, FakeExchange, candles, engine, panel, tick, world  # noqa: F401


def sys_sample(**kw):
    base = {"cpu": 45.0, "ram": 40.0, "disk_pct": 50.0, "disk_free_gb": 100.0, "net_rx_bps": None, "net_tx_bps": None, "threads": 8}
    base.update(kw)
    return base


def metrics(**kw):
    """Métricas agregadas de um equipamento confortável; cada teste muda só o que interessa."""
    m = {"cpu": 45.0, "cpu_max": 50.0, "ram": 40.0, "disk_pct": 50.0, "disk_free_gb": 100.0, "interval": 60.0, "dur_last": 1.8,
         "dur_recent": 1.8, "requests": 40, "failures": 0, "timeouts": 0, "latency": 200.0, "pending": 0, "bots_active": 18}
    m.update(kw)
    return m


def ind(result, key):
    return next(i for i in result["indicators"] if i["key"] == key)


# ---------- avaliação ----------
def test_normal_with_many_bots_when_the_machine_has_margin():
    r = evaluate(metrics())
    assert r["state"] == NORMAL and r["pct"] < 25 and r["advice"].startswith("O equipamento tem margem")
    assert [d["label"] for d in r["display"]] == ["CPU", "RAM", "Disco", "Runner", "Rede", "Fila"]
    assert r["display"][3]["text"] == "Normal" and r["display"][4]["text"] == "Normal"


def test_high_cpu_alone_is_not_critical_when_the_runner_is_on_time():
    r = evaluate(metrics(cpu=96.0))
    assert ind(r, "cpu")["raw"] == 3 and ind(r, "cpu")["level"] == 1          # o CPU sozinho pára em ATENÇÃO
    assert r["state"] == ATENCAO


def test_low_cpu_but_late_runner_and_failing_requests_is_critical():
    r = evaluate(metrics(cpu=12.0, dur_recent=75.0, requests=30, failures=12, timeouts=10))
    assert r["state"] == CRITICO and ind(r, "runner")["level"] == 3 and ind(r, "errors")["level"] >= 2
    assert r["display"][3]["text"].startswith("+15.0")                          # atraso = 75 s − 60 s


def test_high_ram_is_only_serious_when_close_to_exhaustion():
    assert evaluate(metrics(ram=85.0))["state"] == NORMAL                     # um PC normal: 85 % não é um problema
    assert evaluate(metrics(ram=90.0))["state"] == ATENCAO
    assert evaluate(metrics(ram=95.0))["state"] == ELEVADO
    assert evaluate(metrics(ram=98.0))["state"] == CRITICO
    assert evaluate(metrics(ram=90.0, dur_recent=40.0))["state"] == ELEVADO   # com o corredor a sofrer, a RAM alta já pesa


def test_slow_cycle_delay_counts_even_with_a_relaxed_machine():
    ok = evaluate(metrics(dur_recent=1.8))
    late = evaluate(metrics(dur_recent=67.0))                                   # 7 s de atraso
    assert ok["state"] == NORMAL and ind(late, "runner")["level"] >= 2 and STATES.index(late["state"]) >= STATES.index(ELEVADO)
    assert late["display"][3]["text"] == "+7.0 s"
    assert evaluate(metrics(dur_recent=90.0))["state"] == CRITICO


def test_timeouts_raise_the_state_by_rate_or_by_count():
    assert evaluate(metrics(requests=40, failures=1))["state"] == NORMAL       # um soluço não conta
    assert evaluate(metrics(requests=40, failures=3))["state"] == ATENCAO     # 7,5 % dos pedidos
    assert evaluate(metrics(requests=40, failures=5))["state"] == ELEVADO     # 12,5 %
    assert evaluate(metrics(requests=40, failures=16, timeouts=16))["state"] == CRITICO
    few = evaluate(metrics(requests=6, failures=6, timeouts=6))               # poucos pedidos: conta a quantidade
    assert few["state"] == ATENCAO and few["display"][4]["text"].startswith("Com falhas")


def test_queue_and_disk_are_measured_and_a_full_disk_is_critical():
    assert evaluate(metrics(pending=40))["state"] == ELEVADO and evaluate(metrics(pending=80))["state"] == CRITICO
    assert evaluate(metrics(disk_pct=88.0))["state"] == ATENCAO and evaluate(metrics(disk_pct=94.0))["state"] == ELEVADO
    assert evaluate(metrics(disk_pct=99.0, disk_free_gb=0.2))["state"] == CRITICO


def test_missing_measurements_are_skipped_not_invented():
    r = evaluate({k: None for k in ("cpu", "ram", "disk_pct", "disk_free_gb", "interval", "dur_recent", "latency", "pending",
                                    "bots_active")} | {"requests": 0, "failures": 0, "timeouts": 0})
    assert r["state"] == NORMAL and r["indicators"] == []
    assert [d["text"] for d in r["display"]] == ["n/d", "n/d", "n/d", "sem ciclos", "sem pedidos", "n/d"]


def test_several_signals_together_push_the_bar_up_but_only_with_a_system_symptom():
    alone = evaluate(metrics(cpu=70.0, ram=80.0, latency=1000.0))
    together = evaluate(metrics(cpu=70.0, ram=80.0, latency=1000.0, dur_recent=40.0))       # o corredor já anda a 2/3 do tempo
    assert together["pct"] > alone["pct"] and alone["state"] == ATENCAO
    three = evaluate(metrics(cpu=70.0, ram=80.0, latency=1000.0, dur_recent=20.0, pending=12))   # 4 indicadores em ATENÇÃO
    assert three["state"] == ELEVADO                                            # 3+ na mesma faixa sobem de estado


def test_bar_and_state_are_consistent_and_thresholds_are_documented_as_generic():
    for m in (metrics(), metrics(cpu=80.0), metrics(dur_recent=70.0), metrics(ram=99.0)):
        r = evaluate(m)
        assert capacity._state_of(r["pct"]) == r["state"]
    assert set(capacity.TH) >= {"cpu", "ram", "disk", "runner", "err_rate", "latency", "queue"}


# ---------- monitor: histerese e transições ----------
def feed(mon, n, **kw):
    """n leituras iguais; devolve as mudanças de estado que houve."""
    changes = []
    for _ in range(n):
        s = {**sys_sample(), "dur": 1.8, "interval": 60.0, "lag": 0.0, "pending": 0, "bots_active": 10, "requests": 40, "errors": 0,
             "timeouts": 0, "req_ms_avg": 200.0, "req_ms_max": 400.0, "bots_processed": 10, "cycle_errors": 0, "ts": time.time()}
        s.update(kw)
        s["lag"] = max(0.0, s["dur"] - s["interval"])
        _, change = mon.observe(s)
        if change:
            changes.append(change)
    return changes


def test_production_defaults_require_confirmation_to_go_up_and_down():
    mon = Monitor(sampler=object())
    assert (mon.up, mon.down, mon.samples.maxlen) == (2, 5, 10)


def test_state_walks_normal_attention_elevated_critical_and_back_with_confirmation():
    mon = Monitor(sampler=object())
    assert feed(mon, 5) == [] and mon.state == NORMAL
    assert feed(mon, 6, dur=20.0) == [(NORMAL, ATENCAO)]                    # ciclo a 1/3 do intervalo
    assert feed(mon, 6, dur=40.0) == [(ATENCAO, ELEVADO)]                   # a 2/3
    assert feed(mon, 6, dur=95.0) == [(ELEVADO, CRITICO)]                   # já atrasado
    back = feed(mon, 30, dur=1.8)                                           # recupera, mas só depois de confirmar
    assert back == [(CRITICO, NORMAL)] and mon.state == NORMAL


def test_recovery_is_not_declared_before_five_good_evaluations():
    mon = Monitor(sampler=object())
    feed(mon, 5)
    feed(mon, 6, dur=95.0)
    assert mon.state == CRITICO
    seen = []
    for _ in range(4):                                                      # 4 bons: ainda CRÍTICO
        seen += feed(mon, 1)
    assert mon.state == CRITICO and seen == []


def test_a_single_slow_cycle_or_cpu_spike_creates_no_alert():
    mon = Monitor(sampler=object())
    feed(mon, 8)
    assert feed(mon, 1, dur=150.0) == [] and feed(mon, 6) == []            # um ciclo lento isolado (a mediana ignora-o)
    assert feed(mon, 1, cpu=99.0) == [] and feed(mon, 8) == [] and mon.state == NORMAL


def test_two_consecutive_slow_cycles_are_a_real_signal():
    mon = Monitor(sampler=object())
    feed(mon, 8)
    changes = feed(mon, 5, dur=80.0)
    assert changes and mon.state in (ELEVADO, CRITICO)


def test_recovery_needs_several_good_cycles_and_is_not_flapping():
    mon = Monitor(sampler=object())
    feed(mon, 8)
    feed(mon, 6, dur=80.0)
    assert mon.state != NORMAL
    changes = []
    for _ in range(3):                                                      # 3 bons não chegam: ainda não confirma
        changes += feed(mon, 1)
    assert mon.state != NORMAL and not [c for c in changes if c[1] == NORMAL]


# ---------- recolha do sistema ----------
PROC_STAT = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
MEMINFO = "MemTotal:        4000000 kB\nMemFree:          500000 kB\nMemAvailable:    1000000 kB\nBuffers: 1 kB\n"
NET_DEV = ("Inter-|   Receive   |  Transmit\n face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
           "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n")


def test_linux_parsers_read_cpu_memory_and_network():
    busy, total = capacity.parse_proc_stat(PROC_STAT)
    assert total == 1000 and busy == 150                                    # ocupado = total − (parado + iowait)
    pct, total_bytes = capacity.parse_meminfo(MEMINFO)
    assert pct == pytest.approx(75.0) and total_bytes == 4_000_000 * 1024
    assert capacity.parse_net_dev(NET_DEV) == (1000, 2000)                  # sem a interface lo


def test_sampler_computes_cpu_from_two_readings_on_linux_style_counters():
    counters = iter([(100, 1000), (150, 1100)])
    s = capacity.SystemSampler(system="Linux")
    s._cpu_counters = lambda: next(counters)
    s._memory = lambda: (33.0, 8_000_000_000)
    s._network = lambda: None
    first = s.read()
    second = s.read()
    assert first["cpu"] is None and second["cpu"] == pytest.approx(50.0)     # (150−100)/(1100−1000)
    assert second["ram"] == 33.0 and second["disk_pct"] is not None and second["disk_free_gb"] > 0


def test_device_is_described_not_special_cased(monkeypatch):
    monkeypatch.setattr(capacity, "_read", lambda p: "Raspberry Pi 4 Model B Rev 1.4\x00" if "device-tree" in str(p) else "")
    assert capacity.device_info("Linux")["kind"] == "Raspberry Pi"
    monkeypatch.setattr(capacity, "_read", lambda p: "flags : fpu hypervisor\n" if "cpuinfo" in str(p) else None)
    assert capacity.device_info("Linux")["kind"] == "Linux (máquina virtual)"
    monkeypatch.setattr(capacity, "_read", lambda p: None)
    assert capacity.device_info("Linux")["kind"] == "PC / servidor Linux"
    assert capacity.device_info("Windows")["kind"] == "PC Windows" and capacity.device_info("Darwin")["kind"] == "Mac"


@pytest.mark.skipif(platform.system() not in ("Windows", "Linux"), reason="leitura real só em Windows ou Linux")
def test_real_sampler_works_on_this_machine(tmp_path):
    s = capacity.SystemSampler(data_dir=tmp_path)
    s.read()
    time.sleep(0.1)
    r = s.read()
    assert 0 <= r["ram"] <= 100 and 0 <= r["disk_pct"] <= 100 and r["disk_free_gb"] > 0 and r["threads"] >= 1
    assert r["cpu"] is None or 0 <= r["cpu"] <= 100
    info = capacity.device_info()
    assert info["kind"] and info["cpus"] and info["os"]


def test_unknown_platforms_degrade_to_none_instead_of_failing(tmp_path):
    s = capacity.SystemSampler(system="Plan9", data_dir=tmp_path)
    r = s.read()
    assert r["ram"] is None and r["disk_pct"] is not None                   # o que não dá para medir não conta


# ---------- pedidos à Binance ----------
def test_meter_counts_requests_failures_and_timeouts_without_treating_4xx_as_failures():
    netmeter.drain()
    ok = Trader("k", "s", opener=lambda req, timeout=8: _Resp({"serverTime": 1}))
    ok.server_time()
    netmeter.record(50.0)
    def http(code):
        def opener(req, timeout=8):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, _Body(b'{"code": -2013, "msg": "no"}'))
        return opener
    with pytest.raises(TraderError):
        Trader("k", "s", opener=http(400)).open_orders("XYZUSDT")           # resposta normal da exchange
    with pytest.raises(TraderError):
        Trader("k", "s", opener=http(503)).open_orders("XYZUSDT")           # falha do servidor
    with pytest.raises(TraderError):
        Trader("k", "s", opener=lambda req, timeout=8: (_ for _ in ()).throw(OSError())).open_orders("XYZUSDT")   # sem ligação
    m = netmeter.drain()
    assert m["requests"] == 5 and m["errors"] == 2 and m["timeouts"] == 1 and m["ms_max"] >= 50.0
    assert netmeter.drain()["requests"] == 0                                # o contador voltou a zero


class _Body:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d

    def close(self):
        pass


class _Resp:
    def __init__(self, data):
        self._d = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._d


# ---------- ciclo do corredor, alertas, histórico, SQLite ----------
class Script:
    """Sensor de sistema com leituras combinadas de antemão."""

    def __init__(self, **kw):
        self.kw = kw

    def read(self):
        return sys_sample(**self.kw)


@pytest.fixture
def empty(tmp_path):
    conn = db.connect(str(tmp_path / "c.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    return conn


def run_cycles(conn, mon, n, dur=1.8, interval=60.0, start=1_700_000_000.0, result=None, step=60.0):
    out = []
    for i in range(n):
        t = start + i * step
        out.append(capacity.after_cycle(conn, mon, t - dur, t, interval, result or {"bots": 3, "errors": 0}, now=t))
    return out


def alert_rows(conn):
    notify.alerts.init(conn)
    return conn.execute("SELECT * FROM alerts WHERE criterion = 'capacidade' ORDER BY first_ts, key").fetchall()


def test_no_bots_and_no_binance_still_reports_a_normal_state(empty):
    netmeter.drain()
    mon = Monitor(sampler=Script())
    res = run_cycles(empty, mon, 3, result={"bots": 0, "errors": 0})
    assert res[-1]["state"] == NORMAL
    snap = capacity.load_snapshot(empty)
    assert snap["bots_active"] == 0 and snap["state"] == "normal" and snap["display"][4]["text"] == "sem pedidos"
    assert not alert_rows(empty) and snap["device"]["kind"]


def test_alerts_only_on_state_changes_and_with_a_recovery_message(empty, tmp_path, monkeypatch, isolated_notifications):
    keystore.save(tmp_path / "tg.json", "123:token", "999")
    monkeypatch.setattr(notify, "TELEGRAM_FILE", tmp_path / "tg.json")
    netmeter.drain()
    mon = Monitor(sampler=Script(), up=2, down=3)
    run_cycles(empty, mon, 6)                                                # tudo normal: nenhum alerta
    assert not alert_rows(empty)
    run_cycles(empty, mon, 10, dur=70.0, start=1_700_010_000.0)              # o corredor atrasa-se
    rows = alert_rows(empty)
    assert 1 <= len(rows) <= 2 and all(r["severity"] in ("atenção", "alto") for r in rows)
    n = len(rows)
    run_cycles(empty, mon, 10, dur=70.0, start=1_700_020_000.0)              # continua igual: NÃO repete
    assert len(alert_rows(empty)) == n
    run_cycles(empty, mon, 30, dur=1.8, start=1_700_030_000.0)               # recupera
    rows = alert_rows(empty)
    assert rows[-1]["severity"] == "info" and "voltou a NORMAL" in rows[-1]["message"] and mon.state == NORMAL
    sent = " | ".join(isolated_notifications)
    assert "migrar para um equipamento com maior capacidade" in sent or "Monitorize a capacidade" in sent
    assert "VPN" not in sent


def test_the_alert_texts_follow_the_spec_and_never_mention_vpn(empty):
    assert "Monitorize a capacidade antes de adicionar mais bots" in capacity.ADVICE[ATENCAO]
    assert "Considere migrar para um equipamento com maior capacidade" in capacity.ADVICE[ELEVADO]
    assert "Evite adicionar novos bots" in capacity.ADVICE[CRITICO]
    assert not any("vpn" in t.lower() for t in capacity.ADVICE.values())


def test_history_is_aggregated_in_five_minute_rows_and_summarised(empty):
    netmeter.drain()
    mon = Monitor(sampler=Script(cpu=40.0, ram=50.0))
    run_cycles(empty, mon, 40)                                               # 40 ciclos = 40 min de leituras
    rows = empty.execute("SELECT * FROM capacity_history ORDER BY bucket").fetchall()
    assert 7 <= len(rows) <= 9 and rows[0]["samples"] <= 5                    # ~1 linha por 5 min, não 1 por leitura
    now = 1_700_000_000.0 + 40 * 60
    h = capacity.history_summary(empty, hours=24, now=now)
    assert h["cpu_avg"] == pytest.approx(40.0) and h["cpu_max"] == 40.0 and h["ram_avg"] == pytest.approx(50.0)
    assert h["cycle_avg"] == pytest.approx(1.8) and h["cycle_max"] == pytest.approx(1.8) and h["errors"] == 0


def test_retention_drops_old_history(empty):
    mon = Monitor(sampler=Script())
    run_cycles(empty, mon, 6, start=1_700_000_000.0)
    run_cycles(empty, mon, 6, start=1_700_000_000.0 + 20 * 86400)            # 20 dias depois
    assert empty.execute("SELECT MIN(bucket) FROM capacity_history").fetchone()[0] >= 1_700_000_000 + 20 * 86400 - 400


def test_monitoring_is_light_on_sqlite_one_small_snapshot_per_cycle(empty):
    mon = Monitor(sampler=Script())
    changes0 = empty.total_changes
    run_cycles(empty, mon, 10)
    assert empty.total_changes - changes0 <= 10 * 1 + 4                      # 1 snapshot por ciclo (+ 1 linha e alguma limpeza)
    assert len(json.dumps(capacity.load_snapshot(empty))) < 3000             # e pequeno


def test_timeouts_and_a_slow_binance_show_up_in_the_network_row(empty):
    netmeter.drain()
    mon = Monitor(sampler=Script(), up=1)
    for i in range(6):
        for _ in range(8):
            netmeter.record(3500.0, error=True, timeout=True)               # tudo a falhar
        run_cycles(empty, mon, 1, start=1_700_000_000.0 + i * 60)
    snap = capacity.load_snapshot(empty)
    assert snap["display"][4]["text"].startswith("Com falhas") and snap["state"] in ("atencao", "elevado", "critico")


def test_a_broken_monitor_never_breaks_the_runner(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    before = botstore.get(conn, bid)["state"]

    def boom(self, sample):
        raise RuntimeError("medidor avariado")
    monkeypatch.setattr(Monitor, "observe", boom)
    mon = Monitor(sampler=Script())
    assert capacity.after_cycle(conn, mon, time.time() - 1, time.time(), 60.0, {"bots": 1, "errors": 0}) is None   # engole o erro
    assert botstore.get(conn, bid)["state"] == before                       # e não mexeu em nada dos bots
    tick(conn, ex, bid, 2)
    assert engine(conn, bid).status == "running"


class Stop(BaseException):
    pass


def test_runner_loop_measures_each_cycle_without_changing_what_the_bots_do(world, monkeypatch):
    conn, ex, bid = world
    path = db_path(conn)
    ex.now = T0 + 1 * 60_000
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        raise Stop()                                                        # sai depois do 1.º ciclo
    monkeypatch.setattr(runner.time, "sleep", fake_sleep)
    monkeypatch.setattr(capacity.SystemSampler, "read", lambda self: sys_sample())
    with pytest.raises(Stop):
        runner.loop(path, lambda: ex, every=60, trader_factory=lambda: ex)
    assert sleeps == [60]
    assert engine(conn, bid).status == "running" and ex.orders                # o bot trabalhou normalmente
    snap = capacity.load_snapshot(conn)
    assert snap["state"] == "normal" and snap["cycle"]["bots"] == 1 and snap["cycle"]["interval"] == 60
    assert db.get(conn, "runner_heartbeat")                                 # e o sinal de vida continua a ser dado


def test_tick_all_reports_bots_and_errors_for_the_monitor(world):
    conn, ex, bid = world
    ex.now = T0 + 60_000
    assert runner.tick_all(conn, ex, ex.now, trader=ex) == {"bots": 1, "errors": 0}
    empty_db = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    assert empty_db == 1


def test_monitoring_does_not_disturb_recovery_or_the_emergency_stop(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    mon = Monitor(sampler=Script(cpu=99.0, ram=97.0))                        # máquina no limite: o PARAR TUDO tem de funcionar na mesma
    run_cycles(conn, mon, 4, dur=95.0)
    assert mon.state in (ELEVADO, CRITICO)
    db.set_many(conn, {"emergency_stop": "1"})
    ex.now = T0 + 3 * 60_000
    runner.emergency_sweep(conn, ex, ex.now)
    eng = engine(conn, bid)
    assert eng.status == "stopped" and not ex.open_cids()                   # o monitor só informa: a paragem correu igual
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ---------- simulação de carga: 1, 5, 10, 20, 50 bots ----------
PER_BOT_S = 1.5                                                             # segundos que cada bot custa ao ciclo (simulado)


def simulate(conn, n_bots, cycles=14):
    """O runner a trabalhar para n bots: a duração do ciclo, a CPU, a fila e as falhas crescem com a carga."""
    netmeter.drain()
    mon = Monitor(sampler=Script(cpu=min(99.0, 8.0 + n_bots * 1.9), ram=min(96.0, 25.0 + n_bots * 1.2)), up=2, down=5)
    last = None
    for i in range(cycles):
        for _ in range(n_bots * 2):
            netmeter.record(180.0 + n_bots * 12)
        if n_bots >= 40:
            for _ in range(5):
                netmeter.record(3000.0, error=True, timeout=True)
        t = 1_700_000_000.0 + i * 60
        dur = 0.4 + n_bots * PER_BOT_S
        conn.execute("UPDATE settings SET value = value WHERE 0")
        last = capacity.after_cycle(conn, mon, t - dur, t, 60.0, {"bots": n_bots, "errors": 0}, now=t)
    return last, mon


def test_indicator_reacts_to_load_from_1_to_50_bots(empty):
    results = {}
    for n in (1, 5, 10, 20, 50):
        conn = empty
        notify.alerts.init(conn)
        conn.execute("DELETE FROM alerts")
        conn.commit()
        res, mon = simulate(conn, n)
        results[n] = (res["pct"], res["state"], mon.state)
    pcts = [results[n][0] for n in (1, 5, 10, 20, 50)]
    assert pcts == sorted(pcts) and pcts[0] < pcts[-1]                       # a barra nunca desce quando a carga sobe
    assert results[1][1] == NORMAL and results[5][1] == NORMAL and results[10][1] == NORMAL
    assert STATES.index(results[20][2]) >= STATES.index(NORMAL) and STATES.index(results[50][2]) >= STATES.index(ELEVADO)
    assert results[50][2] in (ELEVADO, CRITICO) and results[50][0] >= 50


def test_more_bots_on_a_faster_machine_can_still_be_normal(empty):
    """O mesmo nº de bots: numa máquina mais rápida (ciclos curtos) fica NORMAL; noutra lenta sobe. Sem limite de bots."""
    fast = evaluate(metrics(bots_active=50, dur_recent=6.0, cpu=40.0, ram=45.0))
    slow = evaluate(metrics(bots_active=12, dur_recent=66.0, cpu=70.0, ram=88.0, requests=40, failures=10, timeouts=8))
    assert fast["state"] == NORMAL and STATES.index(slow["state"]) >= STATES.index(ELEVADO)


# ---------- painel ----------
def test_home_card_and_system_tab_show_capacity_in_the_existing_style(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    html = c.get("/").get_data(as_text=True)
    assert "Capacidade operacional" in html and "SEM MEDIÇÕES" in html       # antes de o corredor medir
    netmeter.drain()
    mon = Monitor(sampler=Script())
    capacity.after_cycle(conn, mon, time.time() - 2, time.time(), 60.0, {"bots": 0, "errors": 0})
    html = c.get("/").get_data(as_text=True)
    assert "● NORMAL" in html and 'class="pill gain"' in html and 'class="bar"' in html
    assert "0 bots ativos" in html and "Ver detalhes" in html
    system = c.get("/configuracao?cat=sistema").get_data(as_text=True)
    for word in ("Equipamento", "Bots ativos", "CPU", "RAM", "Disco", "Runner", "Rede", "Fila", "Último ciclo do corredor",
                 "não aumenta a capacidade"):
        assert word in system, word
    assert "<style" not in html.split("Capacidade operacional")[1][:900]     # sem estilos novos: só as classes que já existem


def test_the_panel_never_shows_an_old_normal_when_the_runner_stopped(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    mon = Monitor(sampler=Script())
    capacity.after_cycle(conn, mon, time.time() - 900, time.time() - 898, 60.0, {"bots": 0, "errors": 0}, now=time.time() - 898)
    html = c.get("/").get_data(as_text=True)
    assert "SEM DADOS RECENTES" in html and "● NORMAL" not in html


def test_panel_shows_the_elevated_state_and_the_migration_advice(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    netmeter.drain()
    mon = Monitor(sampler=Script(cpu=76.0, ram=72.0), up=1)
    for i in range(4):
        capacity.after_cycle(conn, mon, time.time() - 70, time.time(), 60.0, {"bots": 30, "errors": 0}, now=time.time() + i)
    html = c.get("/").get_data(as_text=True)
    assert "● ELEVADO" in html and "Considere migrar para um equipamento com maior capacidade" in html
    assert 'class="pill warn"' in html


def test_the_visual_style_file_was_not_touched():
    import subprocess
    out = subprocess.run(["git", "diff", "--stat", "--", "app/static/app.css"], capture_output=True, text=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    assert out.stdout.strip() == ""                                          # o app.css não foi alterado
