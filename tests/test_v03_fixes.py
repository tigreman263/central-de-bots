"""Correções da revisão da v0.3: quantidade dos degraus, comandos, PARAR TUDO, pó, ids, comissões, regra
'nunca vender abaixo do custo', métricas, segurança do painel e robustez para o Pi. Tudo contra exchanges falsas."""
import io
import os
import json
import re
import urllib.error

import pytest

from app import botstore, config, create_app, db, keystore, notify, runner
from app import trader as trader_mod
from app.engine import MIN, PAUSED, RUNNING, STOPPED, Engine
from app.trader import Trader, TraderError
from test_grid import flat, line, new_engine, run, sine
from test_grid import candles as gcandles
from test_testnet import (PAIR, T0, Crash, FakeExchange, assert_consistent, candles, csrf, engine, first_order,
                          panel, tick, world)


def sell_of(ex, slot):
    return next(c for c, o in ex.orders.items() if o["side"] == "SELL" and o["status"] == "NEW"
                and c.split("-")[1] == f"{slot}s")


# ---------- revisor: a quantidade do degrau não encolhe ----------
def test_step_quantity_does_not_erode_over_many_cycles(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    plan = [sl["qty"] for sl in engine(conn, bid).grid["slots"]]
    k = 2
    for _ in range(12):
        buy = first_order(ex, "buy")
        slot = int(buy.split("-")[1][:-1])
        ex.fill(buy)
        tick(conn, ex, bid, k)
        sell = sell_of(ex, slot)
        assert float(ex.orders[sell]["origQty"]) <= plan[slot] + 1e-9        # vende o que tem (comissão em moeda)
        ex.fill(sell)
        tick(conn, ex, bid, k + 1)
        k += 2
    eng = engine(conn, bid)
    assert [sl["qty"] for sl in eng.grid["slots"]] == plan                   # o plano nunca muda
    assert eng.status == RUNNING and eng.s["cycles"] >= 12
    again = first_order(ex, "buy")
    assert float(ex.orders[again]["origQty"]) == pytest.approx(plan[int(again.split("-")[1][:-1])])
    assert_consistent(conn, ex, bid)


# ---------- revisor: comandos do painel ----------
def test_panel_command_is_applied_before_the_network_and_survives_failures(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    botstore.set_command(conn, bid, "pause")
    ex.fail["open_orders"] = TraderError("Sem ligação à Testnet.")
    tick(conn, ex, bid, 2)
    row = botstore.get(conn, bid)
    assert row["status"] == "paused" and row["command"] is None              # aplicado antes de tocar na rede
    botstore.set_command(conn, bid, "resume")

    def boom(*a, **k):
        raise RuntimeError("erro inesperado")
    monkeypatch.setattr(runner, "_advance", boom)
    tick(conn, ex, bid, 3)
    row = botstore.get(conn, bid)
    assert row["command"] == "resume"                                        # não foi aplicado: não pode ser apagado
    assert [e for e in botstore.events(conn, bid) if e["kind"] == "error"]
    monkeypatch.undo()
    tick(conn, ex, bid, 4)
    assert botstore.get(conn, bid)["command"] is None


def test_a_newer_command_is_never_erased_by_an_older_one(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    botstore.set_command(conn, bid, "stop")
    botstore.clear_command(conn, bid, expected="pause")
    assert botstore.get(conn, bid)["command"] == "stop"
    botstore.clear_command(conn, bid, expected="stop")
    assert botstore.get(conn, bid)["command"] is None


def test_an_error_in_the_middle_of_a_step_never_counts_a_fill_twice(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    ex.fill(first_order(ex, "buy"))
    base_before = engine(conn, bid).s["base"]
    real = Engine.on_exchange_fill

    def half_applied(self, *a, **k):
        real(self, *a, **k)
        raise RuntimeError("morreu a meio")
    monkeypatch.setattr(Engine, "on_exchange_fill", half_applied)
    tick(conn, ex, bid, 2)                                                   # estado a meio: é descartado
    assert engine(conn, bid).s["base"] == pytest.approx(base_before)
    monkeypatch.undo()
    tick(conn, ex, bid, 3)
    assert engine(conn, bid).s["base"] > base_before                         # aplicado uma única vez
    assert_consistent(conn, ex, bid)


# ---------- revisor: PARAR TUDO robusto ----------
def test_emergency_sweep_keeps_going_when_one_bot_fails(world, monkeypatch):
    conn, ex, bid = world
    bid2 = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid2, 1)
    db.set_many(conn, {"emergency_stop": "1"})
    real = runner.emergency_bot

    def flaky(c, trader, bot_id, now_ms, defer_notify=False):
        if bot_id == bid:
            raise RuntimeError("boom")
        return real(c, trader, bot_id, now_ms, defer_notify)
    monkeypatch.setattr(runner, "emergency_bot", flaky)
    runner.emergency_sweep(conn, ex, T0 + 2 * MIN)
    assert engine(conn, bid2).status == STOPPED                              # o outro bot foi parado na mesma


def test_closing_sells_the_current_balance_even_if_a_late_fill_arrived(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    ex.fill(first_order(ex, "buy"))                                          # executa entre o passo e o PARAR TUDO
    db.set_many(conn, {"emergency_stop": "1"})
    runner.emergency_sweep(conn, ex, T0 + 2 * MIN)
    assert engine(conn, bid).status == STOPPED
    assert ex.bal["XYZ"] < 2 * ex.rules["step"] + 1e-9                       # só sobra pó abaixo do passo


# ---------- revisor: recentragem com pó e grelhas antigas ----------
def test_recentering_works_with_dust_and_keeps_the_dust():
    e = run(new_engine(), gcandles(flat(3)))
    for sl in e.grid["slots"]:
        sl["holding"], sl["buy_cost"] = False, 0.0
    e.orders = []
    e.s["quote"] += e.s["base"] * 100
    e.s["base"] = 0.01                                                       # pó de ~1 USDT, abaixo da ordem mínima
    run(e, gcandles([120] * 200, start=e.last_ts + MIN))
    assert any(ev["kind"] == "recenter" for ev in e.new_events)
    assert e.s["base"] == pytest.approx(0.01)                                # o pó continua a contar no saldo


def test_late_fill_from_an_old_grid_is_counted_as_dust_not_on_the_wrong_step():
    rules = {"tick": 0.01, "step": 0.001, "min_notional": 5.0}
    e = Engine.new(PAIR, 77.0, rules, {}, mode="testnet")
    e.id = 5
    e.process_candle((T0, 100, 100, 100, 100))
    old = e.s["gen"]
    e.s["gen"] += 1                                                          # a grelha foi recentrada entretanto
    q0, b0 = e.s["quote"], e.s["base"]
    late = {"slot": 1, "side": "buy", "price": 94.0, "qty": 0.078, "gen": old}
    e.on_exchange_fill(late, T0, 94.0, 0.039, 0.0039, 0.000039, partial=True, cash_fee=0.0)
    assert not e.grid["slots"][1]["holding"]
    assert e.s["base"] == pytest.approx(b0 + 0.039 - 0.000039)
    assert e.s["quote"] == pytest.approx(q0 - 0.039 * 94.0)                  # comissão paga em moeda: USDT só paga o custo
    assert e.new_fills[-1]["slot"] == -2


# ---------- revisor: ids ----------
def test_ids_never_collide_after_recreating_the_database(tmp_path):
    ex = FakeExchange(candles(flat(30)))

    def make(name):
        conn = db.connect(str(tmp_path / f"{name}.db"))
        conn.executescript(db.SCHEMA)
        botstore.init(conn)
        return conn, botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    c1, b1 = make("a")
    tick(c1, ex, b1, 1)
    c2, b2 = make("b")                                                       # base de dados nova: o bot volta a ser o n.º 1
    tick(c2, ex, b2, 1)
    assert b1 == b2
    p1, p2 = engine(c1, b1).cid_prefix(), engine(c2, b2).cid_prefix()
    assert p1 != p2
    own = {o["cid"] for o in engine(c2, b2).orders if o.get("cid")}
    assert own and all(c.startswith(p2) for c in own)
    assert all(n == 1 for n in ex.placed.values())                           # nenhuma ordem "fantasma" do bot antigo


# ---------- revisor: comissões ----------
def test_commission_paid_in_another_coin_is_not_taken_from_the_balance(world):
    conn, ex, bid = world
    ex.commission_asset = "BNB"
    tick(conn, ex, bid, 1)
    eng = engine(conn, bid)
    assert eng.s["quote"] == pytest.approx(eng.grid["budget"] - 0.279 * 100)  # a comissão em BNB não sai dos USDT
    assert eng.s["base"] == pytest.approx(0.279)                              # nem da moeda
    assert eng.s["fees"] > 0                                                  # mas conta para o lucro (estimada)
    assert_consistent(conn, ex, bid)


# ---------- revisor: reset sem ordens abertas, refusas repetidas, envios presos, corte a meio ----------
def test_reset_is_detected_from_the_balance_even_with_no_open_orders(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    eng = engine(conn, bid)
    eng.orders, eng.s["cancel_queue"] = [], []
    ex.reset()
    runner.TestnetExecutor(ex).reconcile(eng, T0 + 3 * 3_600_000)
    assert eng.status == STOPPED and "reset da Testnet" in eng.reason


def test_a_refused_order_is_not_recreated_every_minute(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    ex.fill(first_order(ex, "buy"))
    ex.fail["place"] = TraderError("filtro", -1013)
    tick(conn, ex, bid, 2)
    assert engine(conn, bid).s["refused"]
    for k in range(3, 7):
        tick(conn, ex, bid, k)
    guards = [e for e in botstore.events(conn, bid, 50) if e["kind"] == "guard"]
    assert len(guards) == 1                                                  # um aviso, não um por minuto


def test_orders_stuck_in_sending_pause_the_bot_after_a_few_steps(world):
    conn, ex, bid = world
    ex.place_limit = lambda *a, **k: (_ for _ in ()).throw(TraderError("Sem ligação à Testnet."))
    for k in range(1, 9):
        tick(conn, ex, bid, k)
    row = botstore.get(conn, bid)
    assert row["status"] == "paused"
    assert [e for e in botstore.events(conn, bid, 50) if e["kind"] == "error"]


def test_crash_6_in_the_middle_of_sending_the_orders(world):
    conn, ex, bid = world
    ex.crash_after_places = 2
    with pytest.raises(Crash):
        tick(conn, ex, bid, 1)
    assert 0 < len(ex.orders) < 8
    tick(conn, ex, bid, 2)
    assert_consistent(conn, ex, bid)                                         # sem duplicados nem ordens em falta
    assert ex.rejections == 0


def test_a_late_bot_alert_key_is_unique_per_event(world):
    conn, ex, bid = world
    eng = Engine.new(PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    eng.id = 3
    notify.bot_events(conn, eng, [{"ts": 5, "kind": "guard", "detail": "a"}, {"ts": 5, "kind": "guard", "detail": "b"}], 5)
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE key LIKE 'bot:3:%'").fetchone()[0] == 2


# ---------- regra "nunca vender abaixo do custo" ----------
def test_never_sell_below_cost_keeps_the_coin_on_a_manual_stop():
    e = run(new_engine(never_sell_below_cost=True), gcandles(flat(3)))
    assert e.s["base"] > 0
    base = e.s["base"]
    e.command("stop", e.last_ts, close=80.0)                                 # muito abaixo do custo
    assert e.status == STOPPED and e.s["base"] == base and not e.orders
    assert "Posição mantida" in e.reason
    off = run(new_engine(), gcandles(flat(3)))                               # regra desligada (por defeito): vende
    off.command("stop", off.last_ts, close=80.0)
    assert off.status == STOPPED and off.s["base"] == 0


def test_never_sell_below_cost_still_sells_when_the_position_is_in_profit():
    e = run(new_engine(never_sell_below_cost=True), gcandles(flat(3)))
    e.command("stop", e.last_ts, close=130.0)                                # acima do custo: vender é lucro
    assert e.status == STOPPED and e.s["base"] == 0


def test_automatic_stop_loss_pauses_instead_of_selling_below_cost():
    e = run(new_engine(never_sell_below_cost=True), gcandles(flat(3) + [80.0] * 60))
    assert e.status == PAUSED and e.s["base"] > 0 and e.s["stop_events"] >= 1
    assert not [f for f in e.new_fills if f["slot"] == -1]                   # nenhuma venda de fecho
    off = run(new_engine(), gcandles(flat(3) + [80.0] * 60))
    assert off.status == STOPPED and off.s["base"] == 0                      # sem a regra: o stop-loss vende


# ---------- métricas ----------
def test_stats_split_realized_from_unrealized_and_compare_with_buy_and_hold():
    e = run(new_engine(), gcandles(sine(1500, amp=0.04, period=720)))
    st = botstore.stats(e, e.last_ts, curve=[(0, 77.0), (1, 70.0), (2, 75.0)])
    assert st["unrealized"] == pytest.approx(st["net_profit"] - st["realized"])
    close = e.s["last_close"]
    assert st["hold_pct"] == pytest.approx((close / 100 - 1) * 100)          # comprar e manter desde o preço inicial
    assert st["vs_hold_pct"] == pytest.approx(st["net_pct"] - st["hold_pct"])
    assert st["max_drawdown_pct"] == pytest.approx((77 - 70) / 77 * 100)
    assert st["cycles_per_day"] > 0 and st["avg_cycle"] is not None and st["stop_distance_pct"] > 0
    assert st["fees_pct_of_gross"] is None or 0 <= st["fees_pct_of_gross"] <= 100


# ---------- painel: segurança e confirmações ----------
def test_panel_headers_nonce_and_setup_only_from_this_computer(tmp_path):
    app = create_app({"DATA_DIR": str(tmp_path / "d"), "TESTING": True})
    c = app.test_client()
    assert c.get("/setup", environ_overrides={"REMOTE_ADDR": "192.168.1.9"}).status_code == 403
    assert c.get("/setup", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 200
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/setup").get_data(as_text=True)).group(1)
    weak = c.post("/setup", data={"csrf": tok, "password": "curta1234", "password2": "curta1234"}, follow_redirects=True)
    assert "pelo menos 12 caracteres" in weak.get_data(as_text=True)
    c.post("/setup", data={"csrf": tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    r = c.get("/configuracao")
    csp = r.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in r.get_data(as_text=True)                    # o script inline só corre com o nonce certo
    assert r.headers["X-Content-Type-Options"] == "nosniff" and "frame-ancestors 'none'" in csp
    assert app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() == 12 * 3600
    assert os.stat(tmp_path / "d" / "secret_key").st_size > 0


def test_currency_switch_ignores_requests_from_other_sites(panel):
    c, ex, app, tmp = panel
    c.get("/moeda/EUR", headers={"Sec-Fetch-Site": "cross-site"})
    assert 'aria-current="true">USDT' in c.get("/bots").get_data(as_text=True)
    c.get("/moeda/EUR", headers={"Referer": "http://localhost/bots"})
    assert 'aria-current="true">EUR' in c.get("/bots").get_data(as_text=True)


def test_stopping_a_bot_and_everything_asks_for_confirmation_with_consequences(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    tick(conn, ex, bid, 1)
    tok = csrf(c, f"/bots/{bid}")
    r = c.post(f"/bots/{bid}/comando", data={"csrf": tok, "cmd": "stop"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/bots/{bid}/parar")
    assert botstore.get(conn, bid)["command"] is None                        # sem confirmação nada acontece
    page = c.get(f"/bots/{bid}/parar").get_data(as_text=True)
    assert "Vai cancelar" in page and "ordens abertas" in page
    c.post(f"/bots/{bid}/comando", data={"csrf": tok, "cmd": "stop", "confirm": "1"})
    assert botstore.get(conn, bid)["command"] == "stop"
    assert re.search(r"Vai parar 1 bot e cancelar \d+ ordens? aberta", c.get("/parar-tudo").get_data(as_text=True))


def test_bot_states_have_symbols_and_stopped_is_not_shown_as_an_error(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    conn.execute("UPDATE bots SET status = 'stopped' WHERE id = ?", (bid,))
    conn.commit()
    html = c.get("/bots").get_data(as_text=True)
    assert "■ Parado" in html and 'class="pill idle"' in html and 'class="pill loss"' not in html


def test_bot_can_be_created_with_the_never_sell_below_cost_rule(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    data = {"csrf": csrf(c, "/bots/novo?modo=testnet"), "pair": "ABCUSDT", "capital": "77", "modo": "testnet",
            "never_below_cost": "1", "step": "preview"}
    page = c.post("/bots/novo?modo=testnet", data=data).get_data(as_text=True)
    assert "Nunca vender abaixo do custo: ligado" in page
    c.post("/bots/novo?modo=testnet", data={**data, "step": "create"})
    row = botstore.all_bots(conn)[0]
    assert json.loads(row["params"])["never_sell_below_cost"] is True
    off = {**data, "step": "create"}
    off.pop("never_below_cost")
    c.post("/bots/novo?modo=testnet", data=off)
    assert json.loads(botstore.all_bots(conn)[0]["params"])["never_sell_below_cost"] is False   # desligada por defeito


def test_bot_page_shows_the_new_metrics_and_the_expectation_note(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    for k in range(1, 4):
        tick(conn, ex, bid, k)
    html = c.get(f"/bots/{bid}").get_data(as_text=True)
    for text in ("Realizado e por realizar", "Contra comprar e manter", "Ritmo", "distância ao stop", "validar a mecânica"):
        assert text in html, text


# ---------- Pi: base de dados, configuração, chaves, relógio, rede ----------
def test_database_uses_wal_and_long_lock_waits(tmp_path):
    path = str(tmp_path / "x.db")
    db.init_db(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_data_and_key_folders_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTS_DATA_DIR", str(tmp_path / "dados"))
    monkeypatch.setenv("BOTS_KEYS_DIR", str(tmp_path / "chaves"))
    assert config.data_dir() == tmp_path / "dados" and config.keys_dir() == tmp_path / "chaves"
    monkeypatch.undo()


def test_keys_are_written_privately_and_the_audit_log_is_pruned(tmp_path):
    path = tmp_path / "k" / "binance_testnet.json"
    keystore.save(path, "KEY", "SECRET")
    assert keystore.load(path) == ("KEY", "SECRET")
    assert keystore.protect(path) in (True, False)                           # nunca levanta erro
    conn = db.connect(str(tmp_path / "a.db"))
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO audit (ts, action, detail) VALUES ('2000-01-01T00:00:00', 'velho', '')")
    conn.commit()
    conn.close()
    db.init_db(str(tmp_path / "a.db"))
    conn = db.connect(str(tmp_path / "a.db"))
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'velho'").fetchone()[0] == 0


def test_trader_corrects_its_clock_instead_of_pausing_bots():
    calls = []

    def http_error(code):
        body = io.BytesIO(json.dumps({"code": code, "msg": "clock"}).encode())
        return urllib.error.HTTPError("http://x", 400, "bad", {}, body)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

    def opener(request, timeout=8):
        calls.append(request.full_url)
        if "/api/v3/time" in request.full_url:
            return Resp(json.dumps({"serverTime": 1_700_000_000_000 + 5_000}).encode())
        if len([c for c in calls if "/api/v3/account" in c]) == 1:
            raise http_error(-1021)
        return Resp(json.dumps({"balances": [{"asset": "USDT", "free": "1", "locked": "0"}]}).encode())
    old = dict(trader_mod._OFFSET)
    try:
        assert Trader("k", "s", opener=opener).account()[0]["asset"] == "USDT"
        assert any("/api/v3/time" in c for c in calls) and trader_mod._OFFSET["ms"] != 0
        assert -1021 not in trader_mod.FATAL_CODES
    finally:
        trader_mod._OFFSET.update(old)


def test_trader_turns_truncated_answers_into_a_network_error():
    class Resp(io.BytesIO):
        def __enter__(self):
            return self
    t = Trader("k", "s", opener=lambda request, timeout=8: Resp(b'{"balances": [{"as'))
    with pytest.raises(TraderError) as exc:
        t.account()
    assert exc.value.code is None and "Sem ligação" in str(exc.value)


def test_only_one_runner_can_hold_the_lock(tmp_path):
    from app import lock
    first = lock.acquire(tmp_path / "runner.lock")
    assert first is not None
    assert lock.acquire(tmp_path / "runner.lock") is None                    # o segundo corredor recusa arrancar
    first.close()
    again = lock.acquire(tmp_path / "runner.lock")
    assert again is not None
    again.close()


def test_backup_is_consistent_and_old_copies_are_removed(tmp_path):
    import sqlite3
    import time
    from app import backup
    path = str(tmp_path / "app.db")
    db.init_db(path)
    conn = db.connect(path)
    db.set_many(conn, {"marca": "ola"})
    conn.close()
    dest = tmp_path / "bk"
    dest.mkdir()
    old = dest / "app-19990101-0000.db"
    old.write_bytes(b"velho")
    os.utime(old, (1, 1))
    target = backup.backup(path, dest, keep_days=7, now=time.time())
    assert target.exists() and not old.exists()
    copy = sqlite3.connect(str(target))
    assert copy.execute("SELECT value FROM settings WHERE key = 'marca'").fetchone()[0] == "ola"


def test_watchdog_warns_once_when_the_runner_stops_and_again_when_it_returns(tmp_path, isolated_notifications):
    from datetime import datetime, timezone
    from app import watchdog
    tg = tmp_path / "tg.json"
    keystore.save(tg, "1:tok", "42")
    notify.TELEGRAM_FILE = tg
    conn = db.connect(str(tmp_path / "w.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    botstore.create(conn, PAIR, 77.0, {"tick": 0.01, "step": 0.001, "min_notional": 5.0}, {}, mode="sim")
    now = datetime.now(timezone.utc).timestamp()
    db.set_many(conn, {"runner_heartbeat": datetime.fromtimestamp(now - 600, timezone.utc).isoformat(timespec="seconds")})
    state = tmp_path / "state.json"
    assert watchdog.check(conn, state, now, sender=lambda t, c, m: isolated_notifications.append(m)) == "parado"
    assert watchdog.check(conn, state, now + 60, sender=lambda t, c, m: isolated_notifications.append(m)) is None   # não repete
    db.set_many(conn, {"runner_heartbeat": datetime.fromtimestamp(now + 100, timezone.utc).isoformat(timespec="seconds")})
    assert watchdog.check(conn, state, now + 120, sender=lambda t, c, m: isolated_notifications.append(m)) == "voltou"
    assert [m for m in isolated_notifications if "parou de dar sinal" in m] and \
        [m for m in isolated_notifications if "voltou" in m]


def test_deploy_files_exist_and_point_at_the_right_commands():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    painel = (root / "deploy" / "bots-painel.service").read_text(encoding="utf-8")
    corredor = (root / "deploy" / "bots-corredor.service").read_text(encoding="utf-8")
    assert "waitress-serve" in painel and "Restart=always" in painel and "NoNewPrivileges=true" in painel
    assert "run_bots.py" in corredor and "time-sync.target" in corredor and "User=bots" in corredor
    for name in ("bots.env.example", "install_pi.sh", "bots-backup.timer", "bots-vigia.timer"):
        assert (root / "deploy" / name).exists(), name
    req = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "waitress" in req and "pytest" not in req                          # o Pi não precisa de pytest
