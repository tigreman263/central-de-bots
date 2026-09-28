"""Fase 2: PARAR TUDO seguro. Contra a exchange FALSA em memória (nenhum teste fala com a Binance).

RUNNING → STOPPING → STOPPED: depois de observada a paragem não nasce nenhuma ordem de estratégia; STOPPED só depois de
confirmado na exchange (sem ordens abertas, sem ordens por enviar e posição fechada); com pó legítimo fica STOPPED com
`stop_outcome == "residual"`. Testes E1-E18 e invariantes STOP-1..STOP-10 (ver docs/FASE2.md).
"""
import time

import pytest

from app import botstore, db, keystore, notify, runner
from app.engine import MIN, RECOVERING, RUNNING, STOPPED, STOPPING
from app.trader import TraderError
from test_recovery import all_text, check_invariants, events, reopen, wipe_local_orders
from test_testnet import PAIR, T0, Crash, FakeExchange, candles, csrf, engine, first_order, panel, tick, world  # noqa: F401


def flag(conn, on=True):
    db.set_many(conn, {"emergency_stop": "1" if on else "0"})


def sweep(conn, ex, k, extra_ms=0):
    ex.now = T0 + k * MIN
    runner.emergency_sweep(conn, ex, ex.now + extra_ms)


def strategy_ids(ex):
    """Ordens de estratégia enviadas à exchange (a ordem de emergência que fecha a posição tem o degrau -1 e a letra l)."""
    return {c for c in ex.placed if "--1l-" not in c}


def market_sells(ex):
    return [o for o in ex.orders.values() if o["side"] == "SELL" and o["type"] == "MARKET"]


def status(conn, bid):
    return botstore.get(conn, bid)["status"]


def closed_out(ex, bots=1):
    """Sem ordens abertas e sem moeda para além do pó abaixo do passo (cada bot pode deixar até um passo)."""
    return not ex.open_cids() and ex.bal["XYZ"] < bots * 2 * ex.rules["step"] + 1e-9


def flatten(conn, ex, bid, base, residual=0.0, residual_cost=0.0, holding=None):
    """Deixa o bot só com `base` de moeda (e, opcionalmente, um degrau com posição): cancela tudo na exchange e ajusta a conta."""
    for cid in list(ex.open_cids()):
        ex.orders[cid]["status"] = "CANCELED"
    eng = engine(conn, bid)
    eng.orders, eng.s["cancel_queue"] = [], []
    for sl in eng.grid["slots"]:
        sl["holding"], sl["buy_cost"] = False, 0.0
        sl.pop("hold_qty", None)
    if holding:
        eng.grid["slots"][0].update(holding=True, buy_cost=holding[1], hold_qty=holding[0])
    eng.s["base"], eng.s["residual_qty"], eng.s["residual_cost"] = base, residual, residual_cost
    ex.bal["XYZ"] = base
    botstore.save_engine(conn, eng)
    return eng


def running(conn, ex, bid, ticks=(1, 2)):
    for k in ticks:
        tick(conn, ex, bid, k)


# ---------- E1-E4: a paragem chega antes / entre / depois / durante os POST ----------
def test_e1_stop_before_the_first_post_sends_nothing(world):
    conn, ex, bid = world
    flag(conn)
    tick(conn, ex, bid, 1)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and eng.reason == "PARAR TUDO" and not ex.orders and not ex.placed


def test_e2_stop_between_two_posts_blocks_every_later_post(world):
    conn, ex, bid = world
    ex.hooks["get_order"] = lambda e: flag(conn) if e.calls["get_order"] == 3 else None   # aparece antes do 3.º POST
    tick(conn, ex, bid, 1)
    assert len(strategy_ids(ex)) == 2                                        # STOP-1: só saíram as 2 anteriores
    eng = engine(conn, bid)
    assert eng.status == STOPPED and closed_out(ex)                           # e a posição da compra inicial foi fechada
    assert "ORDER_IN_FLIGHT_DURING_STOP" not in all_text(conn, bid)
    assert not [o for o in eng.orders] and not eng.s["cancel_queue"]


def test_e3_stop_after_the_first_post_leaves_only_that_order(world):
    conn, ex, bid = world
    ex.hooks["get_order"] = lambda e: flag(conn) if e.calls["get_order"] == 2 else None   # depois de o 1.º POST terminar
    tick(conn, ex, bid, 1)
    assert len(strategy_ids(ex)) == 1                                        # a compra inicial ao mercado
    assert engine(conn, bid).status == STOPPED and closed_out(ex) and len(market_sells(ex)) == 1


def test_e4_stop_during_a_post_marks_the_order_in_flight_and_cancels_it(world):
    conn, ex, bid = world
    ex.after_hooks["place"] = lambda e: flag(conn) if e.calls["place"] == 2 else None    # a paragem chega durante o POST
    tick(conn, ex, bid, 1)
    assert len(strategy_ids(ex)) == 2                                        # nenhum 3.º POST
    assert "ORDER_IN_FLIGHT_DURING_STOP" in all_text(conn, bid)
    limit = next(o for o in ex.orders.values() if o["type"] == "LIMIT")
    assert limit["status"] == "CANCELED"                                     # reconciliada e cancelada
    assert engine(conn, bid).status == STOPPED and closed_out(ex)


# ---------- E5-E8: cancelamentos ----------
def test_e5_cancel_ok_stops_only_after_confirming_the_exchange(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    assert len(ex.open_cids()) == 6
    flag(conn)
    sweep(conn, ex, 3)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and eng.s["stop_outcome"] in ("closed", "residual") and closed_out(ex)
    assert not eng.orders and not eng.s["cancel_queue"] and eng.reason == "PARAR TUDO"
    assert any("confirmado na Testnet" in d for _, d in events(conn, bid, ("stop",)))
    check_invariants(conn, ex, bid)


def test_e6_cancel_answered_unknown_order_because_it_was_filled_is_booked_not_assumed_cancelled(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    buy = first_order(ex, "buy")
    Q = float(ex.orders[buy]["origQty"])
    ex.fill_on_cancel[buy] = None                                            # executa toda durante o DELETE → -2011
    base0 = engine(conn, bid).s["base"]
    flag(conn)
    sweep(conn, ex, 3)
    assert ex.orders[buy]["status"] == "FILLED"
    rows = conn.execute("SELECT * FROM bot_trade_registry WHERE bot_id = ? AND cid = ?", (bid, buy)).fetchall()
    assert len(rows) == 1 and rows[0]["qty"] == pytest.approx(Q)             # STOP-5: contabilizado uma só vez
    eng = engine(conn, bid)
    assert eng.status == STOPPED and closed_out(ex)                          # e a moeda dessa compra também foi vendida
    assert len(market_sells(ex)) == 1 and float(market_sells(ex)[0]["origQty"]) <= base0 + Q + 1e-9
    check_invariants(conn, ex, bid)


def test_e6b_unknown_order_on_cancel_looks_the_order_up_and_books_the_fill_at_once(world):
    """Sem esperar por nenhuma reconciliação: o próprio cancelamento consulta a ordem e contabiliza o fill."""
    from app.executor import TestnetExecutor
    conn, ex, bid = world
    running(conn, ex, bid)
    buy = first_order(ex, "buy")
    Q = float(ex.orders[buy]["origQty"])
    eng = engine(conn, bid)
    base0 = eng.s["base"]
    item = {"cid": buy, "oid": ex.orders[buy]["orderId"], "slot": int(buy.split("-")[1][:-1]), "side": "buy",
            "price": float(ex.orders[buy]["price"]), "qty": Q, "gen": eng.s["gen"]}
    eng._set_orders([o for o in eng.orders if o["cid"] != buy])              # o bot larga a ordem: vai para a fila
    ex.fill_on_cancel[buy] = None                                            # mas ela executa durante o DELETE
    TestnetExecutor(ex)._cancel_one(eng, item, T0 + 3 * MIN)
    assert [t["cid"] for t in eng.new_trades] == [buy] and eng.new_trades[0]["qty"] == pytest.approx(Q)
    assert eng.s["base"] == pytest.approx(base0 + Q * 0.999)                 # entrou na posição, não ficou "cancelada"


def test_e7_partial_fill_then_cancel_books_the_part_and_sells_only_what_exists(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    buy = first_order(ex, "buy")
    Q = float(ex.orders[buy]["origQty"])
    ex.fill_on_cancel[buy] = 0.4 * Q                                         # 40% executam durante o cancelamento
    flag(conn)
    sweep(conn, ex, 3)
    rows = conn.execute("SELECT * FROM bot_trade_registry WHERE bot_id = ? AND cid = ?", (bid, buy)).fetchall()
    assert len(rows) == 1 and rows[0]["qty"] == pytest.approx(0.4 * Q)       # STOP-5
    assert ex.orders[buy]["status"] == "CANCELED"                            # o resto (60%) foi cancelado
    eng = engine(conn, bid)
    assert eng.status == STOPPED and closed_out(ex) and ex.bal["XYZ"] >= 0   # nunca vendeu mais do que existia
    check_invariants(conn, ex, bid)


def test_e8_cancel_failure_keeps_stopping_and_never_declares_stopped(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    ex.persist_fail["cancel"] = TraderError("Sem ligação à Testnet.")
    flag(conn)
    for k in (3, 4, 5):
        sweep(conn, ex, k)
        assert status(conn, bid) == STOPPING and ex.open_cids()              # STOP-2 e STOP-6
        assert engine(conn, bid).s["cancel_queue"]                           # o cancelamento fica na fila, persistente
    del ex.persist_fail["cancel"]
    sweep(conn, ex, 6)
    assert status(conn, bid) == STOPPED and closed_out(ex)


# ---------- E9, E12, E17: exchange em baixo, reinício, prazo ----------
def test_e9_binance_offline_keeps_stopping_blocks_orders_and_finishes_when_back(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    placed = dict(ex.placed)
    ex.offline = True
    flag(conn)
    for k in (3, 4):
        sweep(conn, ex, k)
        row = botstore.get(conn, bid)
        assert row["status"] == "stopping"                                   # STOP-7: nada de STOPPED falso
        assert "Sem resposta da Testnet" in row["warning"]
    assert ex.placed == placed                                               # e não nasce nenhuma ordem
    ex.offline = False
    sweep(conn, ex, 5)
    assert status(conn, bid) == STOPPED and closed_out(ex)


def test_e12_restart_during_stopping_continues_the_stop_and_never_goes_back_to_running(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    ex.offline = True
    flag(conn)
    sweep(conn, ex, 3)
    assert status(conn, bid) == STOPPING
    conn = reopen(conn)                                                      # o processo reinicia
    tick(conn, ex, bid, 4)                                                   # ainda sem rede: continua STOPPING
    assert status(conn, bid) == STOPPING and not [c for c in ex.placed if c not in ex.orders]
    flag(conn, False)                                                        # nem desligar a flag o faz voltar atrás
    ex.offline = False
    n_strategy = len(strategy_ids(ex))
    conn = reopen(conn)
    tick(conn, ex, bid, 5)
    assert status(conn, bid) == STOPPED and closed_out(ex)                   # STOP-8
    tick(conn, ex, bid, 6)
    assert status(conn, bid) == STOPPED and len(strategy_ids(ex)) == n_strategy


def test_e17_deadline_raises_a_critical_alert_but_never_declares_stopped(world):
    conn, ex, bid = world
    assert runner.stop_deadline_ms(conn) == 120_000                          # por defeito: 120 s
    db.set_many(conn, {"emergency_deadline_s": "10"})
    assert runner.stop_deadline_ms(conn) == 10_000
    running(conn, ex, bid)
    ex.offline = True
    flag(conn)
    sweep(conn, ex, 3)
    assert not [d for _, d in events(conn, bid) if "CRÍTICO" in d]
    sweep(conn, ex, 3, extra_ms=11_000)                                      # passou o prazo
    sweep(conn, ex, 3, extra_ms=12_000)                                      # mesmo prazo: não repete
    assert len([d for _, d in events(conn, bid) if "CRÍTICO" in d]) == 1
    sweep(conn, ex, 3, extra_ms=25_000)                                      # outro prazo: alerta outra vez
    assert len([d for _, d in events(conn, bid) if "CRÍTICO" in d]) == 2
    assert status(conn, bid) == STOPPING and "NÃO está parado" in all_text(conn, bid)
    ex.offline = False
    sweep(conn, ex, 4)
    assert status(conn, bid) == STOPPED


# ---------- E10, E11: alertas lentos e vários bots ----------
def three_bots(conn, ex, bid):
    bids = [bid] + [botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet") for _ in range(2)]
    for b in bids:
        running(conn, ex, b)
    return bids


def test_e10_slow_telegram_does_not_block_the_stop(world, tmp_path, monkeypatch):
    conn, ex, bid = world
    bids = three_bots(conn, ex, bid)
    keystore.save(tmp_path / "tg.json", "123:token", "999")
    monkeypatch.setattr(notify, "TELEGRAM_FILE", tmp_path / "tg.json")
    got = []

    def slow(token, chat, text):
        time.sleep(0.3)                                                      # cada mensagem demora 0,3 s
        got.append(text)
        return True, ""

    monkeypatch.setattr(notify, "SENDER", slow)
    monkeypatch.setattr(notify, "_QUEUE", None)
    notify.start_worker()                                                    # como o corredor: envio em segundo plano
    flag(conn)
    t0 = time.time()
    sweep(conn, ex, 3)
    elapsed = time.time() - t0
    assert all(status(conn, b) == STOPPED for b in bids)
    assert elapsed < 0.9                                                     # STOP-9 (síncrono levaria > 1,8 s)
    deadline = time.time() + 10
    while len(got) < 3 and time.time() < deadline:
        time.sleep(0.05)
    assert len(got) >= 3                                                     # e as mensagens acabaram por chegar


def test_e11_many_bots_all_enter_stopping_even_if_one_fails_or_is_slow(world, monkeypatch):
    conn, ex, bid = world
    bids = three_bots(conn, ex, bid)
    real = runner.emergency_bot

    def flaky(c, trader, bot_id, now_ms, defer_notify=False):
        if bot_id == bids[1]:
            raise RuntimeError("boom")
        return real(c, trader, bot_id, now_ms, defer_notify)

    monkeypatch.setattr(runner, "emergency_bot", flaky)
    flag(conn)
    sweep(conn, ex, 3)
    assert status(conn, bids[1]) == STOPPING                                 # entrou em STOPPING (1.ª fase, sem rede)
    assert status(conn, bids[0]) == STOPPED and status(conn, bids[2]) == STOPPED
    monkeypatch.setattr(runner, "emergency_bot", real)
    sweep(conn, ex, 4)                                                       # o problema passou: o bot que falhou também acaba
    assert all(status(conn, b) == STOPPED for b in bids) and closed_out(ex, bots=3)
    assert runner.bot_lock(bids[0]) is not runner.bot_lock(bids[1])         # um fecho por bot, não global


def test_e11b_one_bot_talking_slowly_does_not_hold_the_others_hostage(world):
    conn, ex, bid = world
    bids = three_bots(conn, ex, bid)
    flag(conn)
    ex.latency["delete"] = 0.02                                              # cada cancelamento demora
    t0 = time.time()
    sweep(conn, ex, 3)
    assert all(status(conn, b) == STOPPED for b in bids) and time.time() - t0 < 5


# ---------- E13-E14: pó e posição desconhecida ----------
def test_e13_residual_below_the_minimum_ends_stopped_with_residual_and_keeps_the_cost(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    flatten(conn, ex, bid, 0.01, residual=0.01, residual_cost=1.0)
    realized, cycles = engine(conn, bid).s["realized"], engine(conn, bid).s["cycles"]
    flag(conn)
    sweep(conn, ex, 3)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and eng.s["stop_outcome"] == "residual"     # STOP-10: acaba, não tenta para sempre
    assert eng.s["residual_qty"] == pytest.approx(0.01) and eng.s["residual_cost"] == pytest.approx(1.0)
    assert eng.s["realized"] == realized and eng.s["cycles"] == cycles       # o pó não vira perda
    assert not market_sells(ex) and eng.position()["kind"] == "residual"
    assert any("sobra" in d for _, d in events(conn, bid, ("stop",)))
    tick(conn, ex, bid, 4)
    assert status(conn, bid) == STOPPED


def test_e13b_a_holding_step_too_small_to_sell_becomes_residual_with_its_cost(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    flatten(conn, ex, bid, 0.02, holding=(0.02, 2.1))                        # 0,02 moeda (≈ 2 USDT) num degrau
    realized = engine(conn, bid).s["realized"]
    flag(conn)
    sweep(conn, ex, 3)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and eng.s["stop_outcome"] == "residual" and not market_sells(ex)
    assert eng.s["residual_qty"] == pytest.approx(0.02) and eng.s["residual_cost"] == pytest.approx(2.1)
    assert not any(sl["holding"] for sl in eng.grid["slots"]) and eng.s["realized"] == realized


def test_e14_unknown_position_is_never_sold(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    local = engine(conn, bid).s["base"]
    ex.bal["XYZ"] += 5.0                                                     # 5 moedas que nenhum bot explica
    flag(conn)
    sweep(conn, ex, 3)
    eng = engine(conn, bid)
    assert eng.status == STOPPED
    assert all(float(o["origQty"]) <= local + 1e-9 for o in market_sells(ex))    # STOP-4: só vendeu o do bot
    assert ex.bal["XYZ"] == pytest.approx(5.0, abs=2 * ex.rules["step"])     # as 5 continuam na conta
    assert [k for k, d in events(conn, bid) if k == "guard" and "UNKNOWN_POSITION" in d]      # com alerta


def test_e14b_bot_without_position_and_coin_on_the_exchange_sells_nothing(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    flatten(conn, ex, bid, 0.0)
    ex.bal["XYZ"] = 5.0
    flag(conn)
    sweep(conn, ex, 3)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and eng.s["stop_outcome"] == "closed" and not market_sells(ex)
    assert ex.bal["XYZ"] == 5.0 and "UNKNOWN_POSITION" in all_text(conn, bid)


def test_stop3_a_closable_position_keeps_stopping_until_it_is_really_closed(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    ex.persist_fail["place"] = TraderError("Sem ligação à Testnet.")         # cancela bem, mas não consegue vender
    flag(conn)
    for k in (3, 4):
        sweep(conn, ex, k)
        assert status(conn, bid) == STOPPING and ex.bal["XYZ"] > 0.1         # STOP-3
        assert not ex.open_cids()
    del ex.persist_fail["place"]
    sweep(conn, ex, 5)
    assert status(conn, bid) == STOPPED and closed_out(ex)


# ---------- E15-E16, E18: recuperação, ordens sending, paragem a meio de um passo ----------
def test_e15_recovery_and_emergency_stop_together_create_no_orders_and_end_stopped(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    wipe_local_orders(conn, bid)                                             # 6 ordens na exchange sem dono local
    ex.fail["all_orders"] = TraderError("Sem ligação à Testnet.")
    tick(conn, ex, bid, 3)
    assert engine(conn, bid).status == RECOVERING
    before = strategy_ids(ex)
    flag(conn)
    sweep(conn, ex, 4)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and strategy_ids(ex) == before              # não iniciou nenhuma grelha nova
    assert closed_out(ex) and not eng.orders
    check_invariants(conn, ex, bid)                                          # e a contabilidade da Fase 1 fecha certa


def test_e16_orders_waiting_to_be_sent_are_dropped_by_the_stop_not_sent(world, monkeypatch):
    conn, ex, bid = world

    def boom(*a, **k):
        raise Crash()

    with monkeypatch.context() as m:
        m.setattr(runner, "_flush", boom)
        with pytest.raises(Crash):
            tick(conn, ex, bid, 1)                                           # ordens gravadas como 'sending'; nada saiu
    assert any(o.get("state") == "sending" for o in engine(conn, bid).orders) and not ex.placed
    flag(conn)
    sweep(conn, ex, 2)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and not ex.placed and not eng.orders and not eng.s["cancel_queue"]


def test_e18_stop_switched_on_during_a_tick_blocks_the_orders_that_tick_was_about_to_send(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    ex.fill(first_order(ex, "buy"))                                          # uma compra executa: o passo vai criar uma venda
    ex.hooks["klines"] = lambda e: flag(conn)                                # a flag liga-se a meio do passo (T2)
    placed = set(ex.placed)
    tick(conn, ex, bid, 3)
    assert set(ex.placed) - placed <= {c for c in ex.placed if "--1l-" in c}     # nenhuma ordem de estratégia nova (STOP-1)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and closed_out(ex) and not eng.orders
    check_invariants(conn, ex, bid)


def test_the_command_stop_from_the_panel_uses_the_same_safe_flow(world):
    conn, ex, bid = world
    running(conn, ex, bid)
    ex.latency["get"] = 0.0
    botstore.set_command(conn, bid, "stop")
    ex.hooks["cancel"] = lambda e: None
    tick(conn, ex, bid, 3)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and closed_out(ex) and eng.reason == "pedido do utilizador"


# ---------- concorrência ----------
class SafeExchange:
    """A exchange falsa não é thread-safe: serializa cada chamada (a concorrência que interessa é a dos bots, não a dela)."""

    def __init__(self, inner):
        import threading
        self._inner, self._lock = inner, threading.Lock()

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            with self._lock:
                return attr(*a, **k)
        return call


@pytest.mark.parametrize("round_", range(4))
def test_tick_and_emergency_sweep_at_the_same_time_leave_one_consistent_state(world, round_, monkeypatch):
    import threading
    from test_recovery import db_path
    conn, ex, bid = world
    running(conn, ex, bid)
    path = db_path(conn)
    safe, errors = SafeExchange(ex), []
    active, overlaps, guard = {}, [], threading.Lock()

    def watched(real):                                                       # nunca duas operações de estado no mesmo bot
        def call(c, first, bot_id, *a, **k):
            with guard:
                active[bot_id] = active.get(bot_id, 0) + 1
                if active[bot_id] > 1:
                    overlaps.append(bot_id)
            try:
                return real(c, first, bot_id, *a, **k)
            finally:
                with guard:
                    active[bot_id] -= 1
        return call

    monkeypatch.setattr(runner, "tick_bot", watched(runner.tick_bot))
    monkeypatch.setattr(runner, "emergency_bot", watched(runner.emergency_bot))
    ex.latency = {"get": 0.004, "post": 0.004, "delete": 0.004}             # a exchange demora: as threads cruzam-se de verdade
    flag(conn)
    ex.now = T0 + 3 * MIN

    def worker(fn):
        c = db.connect(path)
        try:
            for _ in range(5):
                fn(c)
        except Exception as exc:                                             # pragma: no cover - só falha se houver corrida
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(lambda c: runner.tick_all(c, safe, ex.now, trader=safe),)),
               threading.Thread(target=worker, args=(lambda c: runner.emergency_sweep(c, safe, ex.now),))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and not any(t.is_alive() for t in threads)
    assert not overlaps                                                      # um fecho por bot: nunca em simultâneo
    eng = check_invariants(conn, ex, bid)
    assert eng.status == STOPPED and closed_out(ex) and not eng.orders and not eng.s["cancel_queue"]
    assert all(n == 1 for n in ex.placed.values())                           # nenhum id enviado duas vezes
    assert len(market_sells(ex)) == 1                                        # e uma só ordem de fecho


# ---------- painel: nunca "tudo parado" antes de estar ----------
def test_panel_shows_stopping_until_the_runner_confirms(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    running(conn, ex, bid)
    ex.offline = True
    r = c.post("/parar-tudo", data={"csrf": csrf(c, "/parar-tudo")}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Tudo parado" not in html and "Paragem pedida" in html
    assert "A PARAR 1 bot" in html and "SISTEMA PARADO" not in html          # o painel não afirma o que ainda não é verdade
    sweep(conn, ex, 3)                                                       # sem rede: continua a parar
    assert status(conn, bid) == STOPPING
    assert "A PARAR 1 bot" in c.get("/bots").get_data(as_text=True) and "A parar" in c.get(f"/bots/{bid}").get_data(as_text=True)
    ex.offline = False
    sweep(conn, ex, 4)
    html = c.get("/bots").get_data(as_text=True)
    assert "SISTEMA PARADO" in html and "A PARAR" not in html


def test_stopping_has_labels_and_a_default_deadline():
    from app import STATES, explain
    assert "stopping" in STATES and "stopping" in explain.STATES
    assert runner.STOP_DEADLINE_S == 120 and db.DEFAULTS["emergency_deadline_s"] == "120"
