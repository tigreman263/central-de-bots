"""Fase 1: recuperação e contabilidade. Tudo contra a exchange FALSA em memória (nenhum teste fala com a Binance).

Cobre: registo de trades (idempotência), fills parciais incrementais, trades atrasados/incompletos, reinícios, reset com
ordens abertas, zero ordens locais, pó explícito, saldos nos dois sentidos, restauro de cópias antigas, migração da base
de dados e os invariantes I1-I9 (ver docs/FASE1.md).
"""
import glob
import os
import random
import shutil
import sqlite3

import pytest

from app import botstore, db, runner
from app.engine import MIN, PAUSED, RECOVERING, RUNNING, STOPPED, Engine, parse_cid
from app.executor import (CANCELLED_NOT_BOOKED, FILLED_NOT_BOOKED, KNOWN_MATCH, KNOWN_MISSING, RECONCILIATION_PENDING,
                          UNKNOWN_BOT_ORDER, UNKNOWN_POSITION, TestnetExecutor)
from app.trader import TraderError
from test_grid import flat
from test_testnet import FEE, PAIR, T0, FakeExchange, candles, engine, first_order, tick, world  # noqa: F401

HOUR_TICKS = 61          # passos de 1 minuto: mais de uma hora, para disparar a verificação horária de saldos


def db_path(conn):
    return next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")


def reopen(conn):
    """Reinício do processo: fecha a ligação e abre a base de dados outra vez."""
    path = db_path(conn)
    conn.close()
    return db.connect(path)


def registry(conn, bid, cid=None):
    sql, args = "SELECT * FROM bot_trade_registry WHERE bot_id = ?", [bid]
    if cid:
        sql, args = sql + " AND cid = ?", args + [cid]
    return conn.execute(sql, args).fetchall()


def events(conn, bid, kinds=("recovery", "recon", "guard", "info")):
    return [(e["kind"], e["detail"]) for e in botstore.events(conn, bid, 200) if e["kind"] in kinds]


def all_text(conn, bid):
    return " | ".join(d for _, d in events(conn, bid))


def limit_cids(ex, side, status=("NEW", "PARTIALLY_FILLED")):
    return [c for c, o in ex.orders.items() if o["side"] == side.upper() and o["type"] == "LIMIT" and o["status"] in status]


def check_invariants(conn, ex, bid):
    """I1 contabilizado <= executado; I2 ordem terminada: igual (ou à espera, RECONCILIATION_PENDING); I3 cada trade uma vez."""
    eng = engine(conn, bid)
    pending = {o["cid"] for o in eng.orders if o.get("state") == RECONCILIATION_PENDING}
    rows = registry(conn, bid)
    keys = [(r["cid"], r["trade_id"]) for r in rows]
    assert len(keys) == len(set(keys)), "trade contabilizado duas vezes"                                    # I3
    per = {}
    for r in rows:
        per[r["cid"]] = per.get(r["cid"], 0.0) + r["qty"]
    for cid, o in ex.orders.items():
        if not cid.startswith(f"cb{bid}"):
            continue
        executed, booked = float(o["executedQty"]), per.get(cid, 0.0)
        assert booked <= executed + 1e-9, (cid, booked, executed)                                          # I1
        if o["status"] not in ("NEW", "PARTIALLY_FILLED"):
            assert abs(booked - executed) < 1e-9 or cid in pending, (cid, booked, executed)                # I2
    return eng


def no_duplicate_slots(ex):
    """Na exchange nunca há duas ordens abertas para o mesmo degrau e lado (exposição duplicada)."""
    keys = [c.split("-", 1)[1].rsplit("-", 1)[0] for c in ex.open_cids() if "--1" not in c]
    assert len(keys) == len(set(keys)), sorted(keys)


# ---------- A/C: fills parciais incrementais ----------
def test_partial_fills_30_50_100_are_booked_incrementally_and_only_once(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    Q, px = float(ex.orders[buy]["origQty"]), float(ex.orders[buy]["price"])
    slot = int(buy.split("-")[1][:-1])
    e0 = engine(conn, bid)
    base0, quote0 = e0.s["base"], e0.s["quote"]
    done = 0.0
    for k, frac in enumerate((0.3, 0.2)):                                    # 30% e depois mais 20% = 50%
        ex.fill(buy, qty=Q * frac)
        done += frac
        for repeat in range(3):                                              # o mesmo estado várias vezes: nada muda
            tick(conn, ex, bid, 2 + 3 * k + repeat)
            eng = check_invariants(conn, ex, bid)
            assert eng.s["base"] == pytest.approx(base0 + Q * done * (1 - FEE))
            assert eng.s["quote"] == pytest.approx(quote0 - Q * done * px)
        assert not eng.grid["slots"][slot]["holding"]                        # o degrau só muda quando a ordem termina
    ex.fill(buy)                                                             # os 50% restantes: FILLED
    for repeat in range(3):
        tick(conn, ex, bid, 8 + repeat)
        eng = check_invariants(conn, ex, bid)
        assert eng.s["base"] == pytest.approx(base0 + Q * (1 - FEE))
        assert eng.s["quote"] == pytest.approx(quote0 - Q * px)
    assert eng.grid["slots"][slot]["holding"]
    assert len(registry(conn, bid, buy)) == 3                                # um registo por trade
    assert len([f for f in botstore.fills(conn, bid, 1000) if f["slot"] == slot and f["side"] == "buy"]) == 1
    assert eng.s["base"] == pytest.approx(ex.bal["XYZ"])


def test_partial_sell_fills_are_booked_incrementally_and_the_cycle_closes_once(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    sell = first_order(ex, "sell")
    Q, px = float(ex.orders[sell]["origQty"]), float(ex.orders[sell]["price"])
    e0 = engine(conn, bid)
    base0, quote0 = e0.s["base"], e0.s["quote"]
    ex.fill(sell, qty=Q * 0.4)
    tick(conn, ex, bid, 2)
    eng = check_invariants(conn, ex, bid)
    assert eng.s["base"] == pytest.approx(base0 - Q * 0.4)
    assert eng.s["quote"] == pytest.approx(quote0 + Q * 0.4 * px * (1 - FEE))
    assert eng.s["cycles"] == 0                                              # o ciclo só fecha com a venda completa
    ex.fill(sell)
    for k in (3, 4):
        tick(conn, ex, bid, k)
    eng = check_invariants(conn, ex, bid)
    assert eng.s["cycles"] == 1 and eng.s["base"] == pytest.approx(ex.bal["XYZ"])
    assert len(registry(conn, bid, sell)) == 2


def test_the_same_trade_is_never_booked_twice(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    ex.fill(buy, qty=float(ex.orders[buy]["origQty"]) / 2)
    eng = engine(conn, bid)
    executor = TestnetExecutor(ex)
    executor.reconcile(eng, T0 + 2 * MIN)
    first_base, first_new = eng.s["base"], len(eng.new_trades)
    assert first_new == 1
    for k in (3, 4, 5):                                                      # reconcile repetido, sem gravar
        executor.reconcile(eng, T0 + k * MIN)
    assert eng.s["base"] == first_base and len(eng.new_trades) == first_new
    row = eng.new_trades[0]
    assert eng.register_trades(row["cid"], row["order_id"], "buy", [eng.seen[row["cid"]][row["trade_id"]]], T0) == []
    botstore.save_engine(conn, eng)
    with pytest.raises(sqlite3.IntegrityError):                              # a base de dados também o recusa
        conn.execute("INSERT INTO bot_trade_registry (bot_id, symbol, order_id, cid, trade_id, side, qty, price, quote_qty, "
                     "processed_at) VALUES (?, ?, ?, ?, ?, 'buy', 1, 1, 1, 0)", (bid, PAIR, row["order_id"], row["cid"], row["trade_id"]))


# ---------- C: trades atrasados e incompletos ----------
def filled_in_three(ex, cid, fractions=(0.3, 0.3)):
    Q = float(ex.orders[cid]["origQty"])
    for f in fractions:
        ex.fill(cid, qty=Q * f)
    ex.fill(cid)                                                             # o resto: FILLED
    return Q, ex.orders[cid]["orderId"]


def test_delayed_trades_1_then_2_then_3_keep_the_order_pending_until_complete(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    slot = int(buy.split("-")[1][:-1])
    base0 = engine(conn, bid).s["base"]
    Q, oid = filled_in_three(ex, buy)
    trades = ex.trades[oid]
    ex.visible_trades[oid] = 1                                               # o myTrades só mostra o 1.º trade
    tick(conn, ex, bid, 2)
    eng = check_invariants(conn, ex, bid)
    pending = next(o for o in eng.orders if o["cid"] == buy)
    assert pending["state"] == RECONCILIATION_PENDING and not eng.grid["slots"][slot]["holding"]
    assert eng.s["base"] == pytest.approx(base0 + float(trades[0]["qty"]) * (1 - FEE))    # só o que já existe
    assert "RECONCILIATION_PENDING" in all_text(conn, bid)                  # evento emitido
    ex.visible_trades[oid] = 2
    tick(conn, ex, bid, 3)
    eng = check_invariants(conn, ex, bid)
    assert next(o for o in eng.orders if o["cid"] == buy)["state"] == RECONCILIATION_PENDING
    assert eng.s["base"] == pytest.approx(base0 + (float(trades[0]["qty"]) + float(trades[1]["qty"])) * (1 - FEE))
    ex.visible_trades[oid] = 3
    for k in (4, 5):
        tick(conn, ex, bid, k)
    eng = check_invariants(conn, ex, bid)
    assert eng.grid["slots"][slot]["holding"] and not [o for o in eng.orders if o["cid"] == buy]
    assert eng.s["base"] == pytest.approx(base0 + Q * (1 - FEE)) == pytest.approx(ex.bal["XYZ"])
    assert len(registry(conn, bid, buy)) == 3
    assert sum("RECONCILIATION_PENDING" in d for _, d in events(conn, bid)) == 1     # o aviso saiu uma vez, não a cada passo


def test_incomplete_my_trades_0_078_vs_0_023377_is_flagged_and_corrected_later(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    assert float(ex.orders[buy]["origQty"]) == pytest.approx(0.078)
    base0 = engine(conn, bid).s["base"]
    ex.fill(buy, qty=0.023377)
    ex.fill(buy)                                                             # executedQty = 0.078, FILLED
    oid = ex.orders[buy]["orderId"]
    ex.visible_trades[oid] = 1                                               # o myTrades só devolve 0.023377
    for k in (2, 3, 4):                                                      # o erro nunca se disfarça: continua pendente
        tick(conn, ex, bid, k)
        eng = check_invariants(conn, ex, bid)
        assert next(o for o in eng.orders if o["cid"] == buy)["state"] == RECONCILIATION_PENDING
        assert eng.s["base"] == pytest.approx(base0 + 0.023377 * (1 - FEE))
    del ex.visible_trades[oid]                                               # o resto dos trades aparece
    tick(conn, ex, bid, 5)
    eng = check_invariants(conn, ex, bid)
    assert eng.s["base"] == pytest.approx(base0 + 0.078 * (1 - FEE)) == pytest.approx(ex.bal["XYZ"])
    assert sum(r["qty"] for r in registry(conn, bid, buy)) == pytest.approx(0.078)
    assert not [o for o in eng.orders if o["cid"] == buy]


def test_trades_that_never_show_up_are_adjusted_explicitly_after_six_hours(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    ex.fill(buy, qty=0.03)
    ex.fill(buy)
    ex.visible_trades[ex.orders[buy]["orderId"]] = 1
    tick(conn, ex, bid, 2)
    assert next(o for o in engine(conn, bid).orders if o["cid"] == buy)["state"] == RECONCILIATION_PENDING
    tick(conn, ex, bid, 2 + 6 * 60 + 1)                                      # 6 h depois e o trade continua em falta
    eng = check_invariants(conn, ex, bid)
    assert not [o for o in eng.orders if o["cid"] == buy]
    assert sorted(r["status"] for r in registry(conn, bid, buy)) == ["adjusted", "booked"]      # marcado, nunca escondido
    assert any(k == "guard" and "Ajuste explícito" in d for k, d in events(conn, bid))


def test_restart_between_fills_never_duplicates(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    Q = float(ex.orders[buy]["origQty"])
    base0 = engine(conn, bid).s["base"]
    ex.fill(buy, qty=Q * 0.3)
    tick(conn, ex, bid, 2)
    conn = reopen(conn)                                                      # o processo reinicia
    ex.fill(buy, qty=Q * 0.3)
    tick(conn, ex, bid, 3)
    conn = reopen(conn)
    tick(conn, ex, bid, 4)                                                   # reinício sem nada de novo
    ex.fill(buy)
    conn = reopen(conn)
    tick(conn, ex, bid, 5)
    eng = check_invariants(conn, ex, bid)
    assert eng.s["base"] == pytest.approx(base0 + Q * (1 - FEE)) == pytest.approx(ex.bal["XYZ"])
    assert len(registry(conn, bid, buy)) == 3


# ---------- E: reset com ordens abertas e zero ordens locais ----------
def test_reset_with_open_orders_recognises_the_rest_and_never_stops(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    before = set(ex.open_cids())
    assert len(before) == 6
    history = (len(registry(conn, bid)), len(botstore.fills(conn, bid, 1000)), len(botstore.events(conn, bid, 1000)))
    victim = first_order(ex, "buy")
    del ex.orders[victim]                                                    # 1 das 6 desaparece da exchange
    tick(conn, ex, bid, 3)
    eng = check_invariants(conn, ex, bid)
    assert eng.status == RUNNING and not eng.s.get("recovery") and not eng.s.get("testnet_reset")
    assert (before - {victim}) <= ex.open_cids()                             # as outras 5 continuam vivas e reconhecidas
    assert {o["cid"] for o in eng.orders if o.get("cid")} == ex.open_cids()  # nada na exchange fica por reconhecer (I4)
    assert len(ex.open_cids()) == 6                                          # o degrau que faltava foi refeito
    assert all(n == 1 for n in ex.placed.values())
    no_duplicate_slots(ex)
    text = all_text(conn, bid)
    assert KNOWN_MISSING in text and "Recuperação iniciada" in text and "Recuperação concluída" in text
    now_counts = (len(registry(conn, bid)), len(botstore.fills(conn, bid, 1000)), len(botstore.events(conn, bid, 1000)))
    assert all(a >= b for a, b in zip(now_counts, history))                  # I6: nada do histórico se perdeu
    tick(conn, ex, bid, 4)
    assert len(ex.open_cids()) == 6 and not engine(conn, bid).s.get("recovery")


def wipe_local_orders(conn, bid):
    conn.execute("DELETE FROM bot_orders WHERE bot_id = ?", (bid,))
    conn.commit()


def test_zero_local_orders_and_six_on_the_exchange_recovers_before_creating_any(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    old = set(ex.open_cids())
    assert len(old) == 6
    wipe_local_orders(conn, bid)                                             # local: 0 ordens; exchange: 6 deste bot
    n_orders = len(ex.orders)
    ex.fail["cancel"] = TraderError("Sem ligação à Testnet.")                # a recuperação não consegue acabar já
    tick(conn, ex, bid, 3)
    eng = engine(conn, bid)
    assert eng.status == RECOVERING and botstore.get(conn, bid)["status"] == "recovering"
    assert len(ex.orders) == n_orders and ex.open_cids() == old              # I5: nenhuma ordem nova durante a recuperação
    tick(conn, ex, bid, 4)                                                   # agora acaba
    eng = check_invariants(conn, ex, bid)
    assert eng.status == RUNNING
    assert all(ex.orders[c]["status"] == "CANCELED" for c in old)            # as 6 antigas foram reconhecidas e limpas
    assert {o["cid"] for o in eng.orders if o.get("cid")} == ex.open_cids() and len(ex.open_cids()) == 6   # não são 12
    assert all(n == 1 for n in ex.placed.values())
    assert eng.s["oseq"] >= max(parse_cid(c)[2] for c in ex.orders)          # o contador nunca fica para trás dos ids existentes
    assert UNKNOWN_BOT_ORDER in all_text(conn, bid)
    no_duplicate_slots(ex)


def test_orders_already_waiting_to_be_sent_are_not_sent_while_recovering(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    eng = engine(conn, bid)
    ex._new("buy", "limit", 0.078, 90.0, f"{eng.cid_prefix()}3b-99")         # ordem do bot que o estado local não conhece
    ex.fill(first_order(ex, "buy"))                                          # e uma compra conhecida executa: cria uma venda nova
    ex.fail["all_orders"] = TraderError("Sem ligação à Testnet.")            # a recuperação não chega ao fim
    n_orders = len(ex.orders)
    tick(conn, ex, bid, 3)
    eng = engine(conn, bid)
    assert eng.status == RECOVERING
    assert any(o.get("state") == "sending" for o in eng.orders)              # há uma ordem guardada, à espera de ser enviada
    assert len(ex.orders) == n_orders                                        # I5: e não sai enquanto a recuperação não acabar
    tick(conn, ex, bid, 4)
    eng = check_invariants(conn, ex, bid)
    assert eng.status == RUNNING and not any(o.get("state") == "sending" for o in eng.orders)
    assert len(ex.orders) > n_orders and {o["cid"] for o in eng.orders if o.get("cid")} == ex.open_cids()   # agora sim


def test_an_order_filled_before_recovery_is_booked_from_its_trades(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    buy = first_order(ex, "buy")
    base0 = engine(conn, bid).s["base"]
    ex.fill(buy)                                                             # executa e o estado local perde as ordens
    wipe_local_orders(conn, bid)
    tick(conn, ex, bid, 3)
    eng = check_invariants(conn, ex, bid)
    assert eng.status == RUNNING and eng.s["base"] == pytest.approx(ex.bal["XYZ"])
    assert eng.s["base"] > base0 and FILLED_NOT_BOOKED in all_text(conn, bid)


def test_cancelled_after_partial_fill_unknown_to_the_bot_is_still_booked(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    buy = first_order(ex, "buy")
    ex.fill(buy, qty=float(ex.orders[buy]["origQty"]) / 2)
    ex.orders[buy]["status"] = "CANCELED"                                    # cancelada por fora, a meio
    wipe_local_orders(conn, bid)
    tick(conn, ex, bid, 3)
    eng = check_invariants(conn, ex, bid)
    assert eng.s["base"] == pytest.approx(ex.bal["XYZ"]) and CANCELLED_NOT_BOOKED in all_text(conn, bid)


def test_the_real_testnet_reset_still_stops_and_keeps_all_history(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    before = engine(conn, bid)
    counts = (len(registry(conn, bid)), len(botstore.fills(conn, bid, 1000)))
    ex.reset()                                                               # a Testnet inteira foi reposta
    tick(conn, ex, bid, 3)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and "reset da Testnet" in eng.reason and eng.s["testnet_reset"]
    assert eng.s["realized"] == before.s["realized"] and eng.s["base"] == before.s["base"]       # nada financeiro se apaga (I6)
    assert len(registry(conn, bid)) >= counts[0] and len(botstore.fills(conn, bid, 1000)) >= counts[1]
    tick(conn, ex, bid, 4)
    assert engine(conn, bid).status == STOPPED and not ex.open_cids()


# ---------- D: pó / posição residual ----------
def sell_partly_and_cancel(conn, ex, bid, frac, k):
    sell = first_order(ex, "sell")
    Q = float(ex.orders[sell]["origQty"])
    ex.fill(sell, qty=Q * frac)
    ex.orders[sell]["status"] = "CANCELED"
    tick(conn, ex, bid, k)
    return sell, Q


def test_partial_sale_under_the_minimum_leaves_explicit_residual_without_a_false_loss(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    q0 = engine(conn, bid).s["quote"]
    usdt0 = ex.bal["USDT"]
    sell, Q = sell_partly_and_cancel(conn, ex, bid, 0.9, 3)
    eng = check_invariants(conn, ex, bid)
    slot = int(sell.split("-")[1][:-1])
    assert not eng.grid["slots"][slot]["holding"]                            # o degrau libertou-se
    s = eng.s
    assert 0 < s["residual_qty"] and s["residual_qty"] * float(ex.orders[sell]["price"]) < eng.rules["min_notional"]
    assert s["residual_cost"] > 0                                            # o custo do pó não se perdeu
    sold = [f for f in botstore.fills(conn, bid, 100) if f["side"] == "sell"]
    assert s["realized"] == pytest.approx(sum(f["pnl"] for f in sold))       # realizado = só a parte vendida: sem perda falsa
    assert s["cycles"] == 0 and s["worst_cycle"] == 0.0                      # e o ciclo não conta como perda
    pos = eng.position()
    assert pos["residual_qty"] == pytest.approx(s["residual_qty"]) and pos["kind"] == "closable"
    assert pos["total_qty"] == pytest.approx(pos["tradable_qty"] + pos["residual_qty"], abs=0.006)
    assert s["base"] == pytest.approx(ex.bal["XYZ"])                         # a equity local = a da exchange
    assert s["quote"] - q0 == pytest.approx(ex.bal["USDT"] - usdt0)
    # sobrevive ao reinício e a uma cópia de segurança restaurada
    conn = reopen(conn)
    reopened = engine(conn, bid)
    assert reopened.s["residual_qty"] == pytest.approx(s["residual_qty"]) and reopened.s["residual_cost"] == pytest.approx(s["residual_cost"])
    tick(conn, ex, bid, 4)
    tick(conn, ex, bid, 5)                                                   # reconcilia outra vez: continua igual
    assert engine(conn, bid).s["residual_qty"] == pytest.approx(s["residual_qty"])
    bak = db_path(conn) + ".copia"
    out = sqlite3.connect(bak)
    conn.backup(out)
    out.close()
    restored = engine(db.connect(bak), bid)
    assert restored.s["residual_qty"] == pytest.approx(s["residual_qty"])


def test_the_old_behaviour_would_have_booked_the_residual_as_a_realized_loss(world):
    """Contra-prova: a perda que antes se registava (custo do pó) é exatamente o que agora fica em `residual_cost`."""
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    sell, Q = sell_partly_and_cancel(conn, ex, bid, 0.9, 3)
    eng = engine(conn, bid)
    assert eng.s["residual_cost"] > 0.5 and eng.s["realized"] > -0.5


def test_residual_survives_a_recenter_and_is_sold_with_its_cost_on_liquidation():
    rules = {"tick": 0.01, "step": 0.001, "min_notional": 5.0}
    e = Engine.new(PAIR, 77.0, rules, {}, mode="testnet")
    e.id = 5
    e.process_candle((T0, 100, 100, 100, 100))
    e.orders = []
    for sl in e.grid["slots"]:
        sl["holding"], sl["buy_cost"] = False, 0.0
        sl.pop("hold_qty", None)
    e.s["quote"] += e.s["base"] * 100
    e.s["base"] = 0.0
    e._residual_add(0.02, 2.1)
    e.s["base"] = 0.02
    for k in range(1, 200):                                                  # sobe acima do intervalo: recentra
        e.process_candle((T0 + k * MIN, 120, 120, 120, 120))
    assert any(ev["kind"] == "recenter" for ev in e.new_events)
    assert e.s["base"] == pytest.approx(0.02) and e.s["residual_qty"] == pytest.approx(0.02)
    assert e.s["residual_cost"] == pytest.approx(2.1) and e.position()["kind"] == "residual"
    realized = e.s["realized"]
    o = {"slot": -1, "side": "sell", "price": 120.0, "qty": 0.02, "gen": None}
    e.finish_order(o, T0, False, 0.02, 0.02 * 120, 0.0024, 0.0, 0.0024)      # vende-se o pó (fecho de posição)
    assert e.s["residual_qty"] == 0.0 and e.s["residual_cost"] == 0.0
    assert e.s["realized"] == pytest.approx(realized + 0.02 * 120 - 0.0024 - 2.1)   # o custo do pó entra no lucro da venda


def test_position_kinds_are_distinguishable_for_phase_2():
    rules = {"tick": 0.01, "step": 0.001, "min_notional": 5.0}
    e = Engine.new(PAIR, 77.0, rules, {}, mode="testnet")
    e.id = 5
    e.process_candle((T0, 100, 100, 100, 100))
    for sl in e.grid["slots"]:
        sl["holding"] = False
        sl.pop("hold_qty", None)
    e.s["base"] = 0.0
    assert e.position()["kind"] == "flat"
    e._residual_add(0.01, 1.0)
    e.s["base"] = 0.01
    assert e.position()["kind"] == "residual"
    e.s["base"] = 3.0                                                        # moeda que nenhum degrau nem residual explica
    assert e.position()["kind"] == "unknown" and e.position()["unattributed_qty"] == pytest.approx(2.99)
    e.s["base"] = 0.01
    e.grid["slots"][0].update(holding=True, buy_cost=5.0, hold_qty=0.05)
    e.s["base"] = 0.06
    assert e.position()["kind"] == "closable"


# ---------- F: saldos nos dois sentidos ----------
def hourly(conn, ex, bid, n):
    tick(conn, ex, bid, 2 + HOUR_TICKS * n)
    return engine(conn, bid)


def test_balance_equal_is_a_match_and_stays_quiet(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    for n in (1, 2):
        eng = hourly(conn, ex, bid, n)
        assert eng.s["balance"]["state"] == "match" and eng.status == RUNNING
    assert not [k for k, d in events(conn, bid) if k == "recovery"]
    assert eng.s["balance"]["usdt_state"] == "account_larger"                # USDT verificado: a conta tem mais, sem alarme


def test_balance_local_greater_than_exchange_is_reported_without_a_reset(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    ex.bal["XYZ"] -= 0.02                                                    # um pouco menos do que o bot julga ter (0,2587;
    eng = hourly(conn, ex, bid, 1)                                           # a conta falsa mostra 0,275 = o bloqueado nas vendas)
    assert eng.s["balance"]["state"] == "local_gt_exchange" and eng.status == RUNNING and not eng.s.get("testnet_reset")
    assert "menor do que o bot julga ter" in all_text(conn, bid)


def test_balance_much_lower_than_local_is_a_reset_not_a_loss(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    for cid in limit_cids(ex, "sell"):                                       # (a conta falsa deriva o bloqueado das vendas abertas)
        ex.orders[cid]["status"] = "CANCELED"
    ex.bal["XYZ"] = 0.01
    eng = hourly(conn, ex, bid, 1)
    assert eng.status == STOPPED and eng.s["testnet_reset"] and "reset da Testnet" in eng.reason


def test_balance_exchange_greater_than_local_is_detected_and_never_sold(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)                                                   # referência: a conta não tem moeda a mais
    base0 = engine(conn, bid).s["base"]
    ex.bal["XYZ"] += 5.0                                                     # chega moeda que nenhum bot explica
    eng = hourly(conn, ex, bid, 1)
    assert eng.s["balance"]["state"] == "local_lt_exchange" and eng.s["balance"]["excess"] == pytest.approx(5.0)
    assert eng.status == RUNNING and eng.s["base"] == pytest.approx(base0)   # não é adotada
    assert UNKNOWN_POSITION in all_text(conn, bid) and "Recuperação concluída" in all_text(conn, bid)
    assert not any(o["side"] == "SELL" and o["type"] == "MARKET" for o in ex.orders.values())   # nem vendida
    n_recoveries = sum("Recuperação iniciada" in d for _, d in events(conn, bid))
    eng = hourly(conn, ex, bid, 2)                                           # o mesmo excesso já conhecido: não repete
    assert sum("Recuperação iniciada" in d for _, d in events(conn, bid)) == n_recoveries


def test_balance_usdt_shortage_is_reported_but_never_used_as_a_reset(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    ex.bal["USDT"] = 10.0                                                    # menos USDT do que os bots dizem ter
    eng = hourly(conn, ex, bid, 1)
    assert eng.s["balance"]["usdt_state"] == "local_gt_exchange" and eng.status == RUNNING and not eng.s.get("testnet_reset")
    assert "USDT" in all_text(conn, bid)


def test_two_bots_on_the_same_pair_do_not_trigger_false_recoveries(world):
    conn, ex, bid = world
    bid2 = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    for k in (1, 2):
        tick(conn, ex, bid, k)
        tick(conn, ex, bid2, k)
    for n in (1, 2, 3):
        for b in (bid, bid2):
            tick(conn, ex, b, 2 + HOUR_TICKS * n)
    for b in (bid, bid2):
        eng = engine(conn, b)
        assert eng.status == RUNNING and eng.s["balance"]["state"] == "match"
        assert not [k for k, d in events(conn, b) if k == "recovery"]
    assert engine(conn, bid).s["base"] + engine(conn, bid2).s["base"] == pytest.approx(ex.bal["XYZ"])
    assert not (set(ex.open_cids()) - {o["cid"] for b in (bid, bid2) for o in engine(conn, b).orders if o.get("cid")})


# ---------- G: restauro de uma cópia de segurança antiga ----------
def restore(conn, bak):
    path = db_path(conn)
    conn.close()
    for side in glob.glob(path + "-wal") + glob.glob(path + "-shm"):
        os.remove(side)
    shutil.copy(bak, path)
    return db.connect(path)


def test_restoring_an_old_backup_recognises_old_orders_and_does_not_duplicate(world, tmp_path):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    bak = str(tmp_path / "antiga.db")
    out = sqlite3.connect(bak)
    conn.backup(out)
    out.close()
    old_seq = engine(conn, bid).s["oseq"]
    buy = first_order(ex, "buy")
    ex.fill(buy)                                                             # depois da cópia: uma compra executa...
    tick(conn, ex, bid, 3)
    new_sells = [c for c in limit_cids(ex, "sell") if parse_cid(c)[2] > old_seq]
    assert new_sells                                                         # ...o bot cria uma venda que a cópia nunca viu
    ex.fill(new_sells[0])                                                    # que também executa
    tick(conn, ex, bid, 4)
    assert any(parse_cid(c)[2] > old_seq for c in ex.open_cids())            # e há ordens abertas que a cópia nunca viu
    conn = restore(conn, bak)                                                # RESTAURO: o estado volta ao passo 2
    for k in (5, 6, 7):
        tick(conn, ex, bid, k)
    eng = check_invariants(conn, ex, bid)
    assert eng.status == RUNNING
    assert eng.s["base"] == pytest.approx(ex.bal["XYZ"], abs=2e-3)           # a venda que a cópia não conhecia foi contabilizada
    assert all(n == 1 for n in ex.placed.values())                           # nenhum id enviado duas vezes
    assert {o["cid"] for o in eng.orders if o.get("cid")} == ex.open_cids()  # tudo o que está aberto é reconhecido (I4)
    no_duplicate_slots(ex)
    assert eng.s["oseq"] >= max(parse_cid(c)[2] for c in ex.orders)
    assert FILLED_NOT_BOOKED in all_text(conn, bid) or UNKNOWN_BOT_ORDER in all_text(conn, bid)


def test_restore_of_a_backup_from_before_the_first_step_keeps_the_uid(world, tmp_path):
    conn, ex, bid = world
    uid0 = engine(conn, bid).s["uid"]                                        # o UID grava-se logo na criação
    bak = str(tmp_path / "pendente.db")
    out = sqlite3.connect(bak)
    conn.backup(out)
    out.close()
    for k in (1, 2, 3):
        tick(conn, ex, bid, k)
    old = set(ex.open_cids())
    conn = restore(conn, bak)
    assert engine(conn, bid).status == "pending" and engine(conn, bid).s["uid"] == uid0
    for k in (4, 5, 6):
        tick(conn, ex, bid, k)
    eng = check_invariants(conn, ex, bid)
    assert all(n == 1 for n in ex.placed.values()) and len(ex.open_cids()) == 6      # os mesmos ids foram adotados, não repetidos
    assert {o["cid"] for o in eng.orders if o.get("cid")} == ex.open_cids() == old


def test_orders_with_an_unproven_uid_are_left_alone_until_linked(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    foreign = f"cb{bid}aaaaaa-3b-1"                                          # mesmo número de bot, UID que não conhecemos
    ex._new("buy", "limit", 0.078, 90.0, foreign)
    tick(conn, ex, bid, 3)
    assert ex.orders[foreign]["status"] == "NEW"                             # pode ser de outra instalação: não se toca
    assert botstore.link_prefix(conn, bid, "aaaaaa")                         # o utilizador liga o UID antigo a este bot
    tick(conn, ex, bid, 4)
    assert ex.orders[foreign]["status"] == "CANCELED" and UNKNOWN_BOT_ORDER in all_text(conn, bid)
    other_bot = f"cb{bid + 1}bbbbbb-3b-1"                                    # outro bot: nunca é confundido com este
    ex._new("buy", "limit", 0.078, 89.0, other_bot)
    wipe_local_orders(conn, bid)
    tick(conn, ex, bid, 5)
    assert ex.orders[other_bot]["status"] == "NEW"


def test_an_existing_id_with_a_different_order_gets_a_new_id_instead_of_being_adopted(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    eng = engine(conn, bid)
    old = first_order(ex, "buy")
    clash = {"slot": 0, "side": "buy", "price": 91.0, "qty": 0.078, "active_from": 0, "type": "limit", "cid": old,
             "state": "sending", "oid": None, "exec_qty": 0.0}
    eng.orders = [clash]
    TestnetExecutor(ex).flush(eng, T0 + 2 * MIN)
    assert clash["cid"] != old and clash["state"] in ("open", "partial")
    assert float(ex.orders[clash["cid"]]["price"]) == 91.0 and float(ex.orders[old]["price"]) == 92.0
    assert all(n == 1 for n in ex.placed.values())


# ---------- H: comandos durante a recuperação ----------
def test_stop_and_emergency_stop_are_kept_during_recovery_and_applied_at_the_end(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    wipe_local_orders(conn, bid)
    ex.fail["all_orders"] = TraderError("Sem ligação à Testnet.")
    tick(conn, ex, bid, 3)
    assert engine(conn, bid).status == RECOVERING
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 4)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and not ex.open_cids()                      # a recuperação acabou e o pedido cumpriu-se
    assert eng.s["base"] < 0.0015                                            # e a posição foi fechada


def test_emergency_flag_during_recovery_stops_the_bot_when_it_ends(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    wipe_local_orders(conn, bid)
    ex.fail["all_orders"] = TraderError("Sem ligação à Testnet.")
    tick(conn, ex, bid, 3)
    assert engine(conn, bid).status == RECOVERING
    db.set_many(conn, {"emergency_stop": "1"})
    tick(conn, ex, bid, 4)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and eng.reason == "PARAR TUDO" and not ex.open_cids()


def test_recovering_status_has_labels_in_the_panel():
    from app import STATES, explain
    assert "recovering" in STATES and "recovering" in explain.STATES


# ---------- migração ----------
LEGACY = """
CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT, pair TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    capital_usdt REAL NOT NULL, params TEXT NOT NULL, rules TEXT NOT NULL, grid TEXT, state TEXT NOT NULL DEFAULT '{}',
    last_ts INTEGER NOT NULL DEFAULT 0, command TEXT, warning TEXT NOT NULL DEFAULT '', created_ts TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'sim', source TEXT NOT NULL DEFAULT 'mainnet', twin_of INTEGER);
CREATE TABLE bot_orders (bot_id INTEGER NOT NULL, slot INTEGER NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, qty REAL NOT NULL,
    active_from INTEGER NOT NULL, cid TEXT, oid TEXT, state TEXT, otype TEXT, exec_qty REAL NOT NULL DEFAULT 0, slots TEXT,
    UNIQUE (bot_id, slot, side));
CREATE TABLE bot_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, slot INTEGER NOT NULL,
    side TEXT NOT NULL, price REAL NOT NULL, qty REAL NOT NULL, fee REAL NOT NULL, pnl REAL);
CREATE TABLE bot_events (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE bot_equity (bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, equity REAL NOT NULL);
CREATE TABLE bot_candles (bot_id INTEGER NOT NULL, ts INTEGER NOT NULL, o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
    PRIMARY KEY (bot_id, ts));
"""


def test_migration_of_an_existing_database_keeps_data_and_backs_it_up_first(tmp_path):
    path = str(tmp_path / "app.db")
    raw = sqlite3.connect(path)
    raw.executescript(LEGACY)
    raw.execute("INSERT INTO bots (pair, status, capital_usdt, params, rules, state, created_ts, mode, source) "
                "VALUES ('XYZUSDT', 'running', 77, '{}', '{}', '{\"uid\": \"abc123\", \"oseq\": 9, \"realized\": 1.5}', 'x', 'testnet', 'testnet')")
    raw.execute("INSERT INTO bots (pair, status, capital_usdt, params, rules, created_ts) VALUES ('ABCUSDT', 'stopped', 50, '{}', '{}', 'x')")
    raw.execute("INSERT INTO bot_orders (bot_id, slot, side, price, qty, active_from, cid, state, otype, exec_qty) "
                "VALUES (1, 3, 'sell', 102, 0.07, 0, 'cb1abc123-3s-4', 'partial', 'limit', 0.02)")
    raw.execute("INSERT INTO bot_fills (bot_id, ts, slot, side, price, qty, fee, pnl) VALUES (1, 5, 3, 'sell', 102, 0.07, 0.01, 0.5)")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    botstore.init(conn)
    backups = glob.glob(path + ".antes-do-registo-*.bak")
    assert len(backups) == 1
    old = sqlite3.connect(backups[0])                                        # a cópia é a base ANTES de migrar
    assert "bot_trade_registry" not in {r[0] for r in old.execute("SELECT name FROM sqlite_master")}
    assert old.execute("SELECT count(*) FROM bots").fetchone()[0] == 2
    old.close()
    assert conn.execute("SELECT count(*) FROM bot_trade_registry").fetchone()[0] == 0
    eng = engine(conn, 1)
    assert eng.s["uid"] == "abc123" and eng.s["realized"] == 1.5 and eng.s["registry_from"] > 0
    order = eng.orders[0]
    assert order["cid"] == "cb1abc123-3s-4" and order["exec_qty"] == 0.02 and eng.booked(order["cid"]) == 0   # parcial: nada contado ainda
    assert botstore.get(conn, 2)["mode"] == "sim" and len(botstore.fills(conn, 1, 10)) == 1
    botstore.init(conn)                                                      # arrancar outra vez: não migra nem copia de novo
    assert len(glob.glob(path + ".antes-do-registo-*.bak")) == 1
    assert engine(conn, 1).s["registry_from"] == eng.s["registry_from"]


def test_a_fresh_database_needs_no_backup_and_deleting_a_bot_keeps_its_trade_registry(world, tmp_path):
    conn, ex, bid = world
    assert not glob.glob(db_path(conn) + ".antes-do-registo-*")
    tick(conn, ex, bid, 1)
    tick(conn, ex, bid, 2)
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 3)
    tick(conn, ex, bid, 4)
    n = len(registry(conn, bid))
    assert n >= 2                                                            # a compra inicial e a venda de fecho
    assert botstore.delete(conn, bid)
    assert len(registry(conn, bid)) == n                                     # o registo é auditoria: fica


# ---------- propriedade: fills aleatórios, parciais e atrasados ----------
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_partial_and_delayed_fills_keep_every_invariant(world, seed):
    conn, ex, bid = world
    rnd = random.Random(seed)
    tick(conn, ex, bid, 1)
    q1, u1 = engine(conn, bid).s["quote"], ex.bal["USDT"]
    for k in range(2, 160):
        for cid in limit_cids(ex, "buy") + limit_cids(ex, "sell"):
            if rnd.random() < 0.25:
                o = ex.orders[cid]
                left = float(o["origQty"]) - float(o["executedQty"])
                ex.fill(cid, qty=None if rnd.random() < 0.4 else round(left * rnd.choice((0.2, 0.3, 0.5)), 3) or None)
        for cid, o in ex.orders.items():                                     # o myTrades atrasa-se um trade, às vezes
            n = len(ex.trades.get(o["orderId"], []))
            if n > 1 and rnd.random() < 0.3:
                ex.visible_trades[o["orderId"]] = n - 1
            else:
                ex.visible_trades.pop(o["orderId"], None)
        tick(conn, ex, bid, k)
        eng = check_invariants(conn, ex, bid)
        unbooked_buys = unbooked_sells = 0.0                                 # o que a exchange já fez e ainda não contámos
        for cid, o in ex.orders.items():
            gap = float(o["executedQty"]) - sum(r["qty"] for r in registry(conn, bid, cid))
            if o["side"] == "BUY":
                unbooked_buys += gap
            else:
                unbooked_sells += gap
        assert eng.s["base"] == pytest.approx(ex.bal["XYZ"] - unbooked_buys * (1 - FEE) + unbooked_sells, abs=1e-6)   # identidade
        assert eng.status in (RUNNING, PAUSED)
    ex.visible_trades.clear()                                                # o myTrades acaba por mostrar tudo
    for k in (160, 161, 162):
        tick(conn, ex, bid, k)
    eng = check_invariants(conn, ex, bid)
    assert not [o for o in eng.orders if o.get("state") == RECONCILIATION_PENDING]
    assert eng.s["base"] == pytest.approx(ex.bal["XYZ"], abs=2e-3)           # local = exchange (I9)
    assert eng.s["quote"] - q1 == pytest.approx(ex.bal["USDT"] - u1, abs=1e-6)     # o dinheiro local = o da exchange
    assert all(n == 1 for n in ex.placed.values())
    assert {o["cid"] for o in eng.orders if o.get("cid")} == ex.open_cids()
    no_duplicate_slots(ex)


def test_classification_names_are_the_ones_of_the_spec():
    assert {KNOWN_MATCH, KNOWN_MISSING, UNKNOWN_BOT_ORDER, FILLED_NOT_BOOKED, CANCELLED_NOT_BOOKED, UNKNOWN_POSITION} == {
        "KNOWN_MATCH", "KNOWN_MISSING", "UNKNOWN_BOT_ORDER", "FILLED_NOT_BOOKED", "CANCELLED_NOT_BOOKED", "UNKNOWN_POSITION"}
    assert parse_cid("cb12abc123-4s-9") == (12, "abc123", 9) and parse_cid("cb1abc123--1i-3") == (1, "abc123", 3)
    assert parse_cid("outro") is None and parse_cid("cb1-0b-1") is None
