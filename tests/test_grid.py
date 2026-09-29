"""Critérios da v0.2 (docs/V02.md): grelha em simulação, proteções, retoma sem duplicar. Mercados sintéticos."""
import json
import math

import pytest

from app import grid as G
from app.engine import MIN, PAUSED, RUNNING, STOPPED, Engine

RULES = {"tick": 0.01, "step": 0.001, "min_notional": 5.0}
CAPITAL = 77.0


def candles(path, start=0, wick=0.0003):
    out, prev = [], path[0]
    for k, c in enumerate(path):
        out.append((start + k * MIN, prev, max(prev, c) * (1 + wick), min(prev, c) * (1 - wick), c))
        prev = c
    return out


def run(engine, cs, check=None):
    for c in cs:
        engine.process_candle(c)
        assert len({(o["slot"], o["side"]) for o in engine.orders}) == len(engine.orders), "ordem duplicada"
        if check:
            check(engine, c)
    return engine


def new_engine(**params):
    return Engine.new("XYZUSDT", CAPITAL, RULES, params)


def flat(n, price=100.0):
    return [price] * n


def sine(n, amp=0.05, period=360, center=100.0):
    return [center * (1 + amp * math.sin(2 * math.pi * k / period)) for k in range(n)]


def line(a, b, n):
    return [a + (b - a) * k / (n - 1) for k in range(n)]


# ---------- guarda de risco / montagem ----------
def test_grid_is_built_with_reserve_and_within_limits():
    e = run(new_engine(), candles(flat(3)))
    g = e.grid
    assert e.status == RUNNING and 3 <= g["levels"] <= 8
    assert abs(g["lower"] - 92) < 1e-9 and abs(g["upper"] - 108) < 1e-9
    assert abs(g["stop"] - 92 * 0.975) < 1e-9
    assert abs(e.s["reserve"] - CAPITAL * 0.25) < 1e-9                 # 25% fora da grelha
    assert all(sl["qty"] * g["prices"][sl["i"]] >= RULES["min_notional"] for sl in g["slots"])
    assert all(sl["worst_loss_pct"] <= 2.0 + 1e-9 for sl in g["slots"])  # perda máx. por operação <= 2%
    assert g["step_pct"] >= G.cost_pct() + 0.3


def test_guard_refuses_bad_configurations():
    with pytest.raises(G.GridRefused, match="grelha segura"):
        G.build_grid(100, 10.0, RULES, {})                              # capital pequeno demais
    with pytest.raises(G.GridRefused):
        G.build_grid(100, CAPITAL, RULES, {"range_pct": 0.5})            # degrau não cobre comissões com folga
    with pytest.raises(G.GridRefused, match="perda máxima"):
        G.build_grid(100, 40.0, {**RULES, "min_notional": 9.0}, {"max_loss_trade_pct": 0.3})


# ---------- regras de execução ----------
def test_touching_a_level_does_not_fill_but_passing_it_does():
    e = run(new_engine(), candles(flat(2)))
    buy = min((o for o in e.orders if o["side"] == "buy"), key=lambda o: -o["price"])
    n_before = len(e.new_fills)
    e.process_candle((e.last_ts + MIN, 100, 100, buy["price"], 100))     # só toca
    assert len(e.new_fills) == n_before
    e.process_candle((e.last_ts + MIN, 100, 100, buy["price"] * 0.998, 100))  # passa além da margem
    assert len(e.new_fills) == n_before + 1 and e.new_fills[-1]["side"] == "buy"


def test_never_a_full_cycle_in_one_candle_and_fills_have_fee_and_slippage():
    e = run(new_engine(), candles(flat(2)))
    n0 = len(e.new_fills)
    wild = (e.last_ts + MIN, 100, 110, 90, 100)                          # atravessa quase toda a grelha
    e.process_candle(wild)
    fills = e.new_fills[n0:]
    sides = [(f["slot"], f["side"]) for f in fills]
    assert len(sides) == len(set(sides))                                 # nenhuma ordem executa duas vezes
    for slot in {s for s, _ in sides}:                                   # um degrau nunca faz compra E venda na mesma vela
        assert {side for s, side in sides if s == slot} != {"buy", "sell"}
    for f in fills:
        assert f["fee"] > 0
    buy = next(f for f in fills if f["side"] == "buy")
    order_price = e.grid["prices"][buy["slot"]]
    assert buy["price"] == pytest.approx(order_price * (1 + G.SLIP))     # deslizamento contra nós


def test_profit_is_net_of_fees_and_accounting_is_consistent():
    e = run(new_engine(), candles(sine(3 * 360 * 2, amp=0.05, period=360)))
    s = e.s
    assert s["cycles"] > 0
    assert s["fees"] == pytest.approx(sum(f["fee"] for f in e.new_fills))
    assert s["realized"] == pytest.approx(sum(f["pnl"] for f in e.new_fills if f["pnl"] is not None))
    assert all(f["pnl"] < f["qty"] * f["price"] * 0.05 for f in e.new_fills if f["pnl"] is not None)
    # o dinheiro nunca é criado: capital final = capital inicial + lucro realizado + mais-valia em aberto
    close = e.s["last_close"]
    assert e.equity(close) - CAPITAL == pytest.approx(
        s["realized"] + s["base"] * close - sum(sl["buy_cost"] for sl in e.grid["slots"] if sl["holding"]))


# ---------- cenários de proteção ----------
def test_scenario_lateral_no_protection_fires_and_cycles_complete():
    def no_false_trigger(e, _):
        assert e.status == RUNNING, e.reason
    e = run(new_engine(), candles(sine(3 * 1440, amp=0.05, period=720)), no_false_trigger)   # 3 dias, ±5%, ciclos de 12 h
    assert e.s["cycles"] >= 5 and e.s["stop_events"] == 0 and not e.s["buys_blocked"]
    assert e.s["worst_loss_pct"] <= 2.0


def test_scenario_strong_drop_protections_hold():
    def invariants(e, c):
        price = c[4]
        assert e.s["base"] * price <= 0.65 * CAPITAL + 1e-6, "passou do limite de inventário"
        assert e.s["reserve"] == pytest.approx(CAPITAL * 0.25)           # a reserva de caixa nunca é gasta
    path = flat(60) + line(100, 85, 180) + flat(240, 85)                 # -15% em 3 horas, sem rebote
    e = run(new_engine(), candles(path), invariants)
    assert e.status == STOPPED and "stop-loss" in e.reason
    assert any(ev["kind"] == "pause" for ev in e.new_events)             # pausa por queda rápida antes do stop
    assert e.s["worst_loss_pct"] <= 2.0                                  # nenhuma operação acima da perda máxima
    assert e.equity(85) >= CAPITAL * 0.90                                # perda total limitada (reserva + stop)
    assert e.s["base"] == 0 and e.orders == []


def test_three_buys_in_a_row_block_further_buys_until_a_sell():
    path = flat(30) + line(100, 93, 300)                                 # queda lenta: só compras, sem vendas
    e = run(new_engine(drop_pause_pct=50, inventory_limit_pct=100), candles(path))
    assert e.s["buys_blocked"] and e.s["consec_buys"] == 3
    assert not any(o["side"] == "buy" for o in e.orders)


def test_scenario_strong_rise_recenters_at_most_twice_per_day_and_never_in_a_loop():
    path = flat(30) + line(100, 115, 120) + flat(1500, 115)              # +15% e fica lá
    e = run(new_engine(), candles(path))
    assert e.status in (RUNNING, PAUSED)
    assert sum(1 for ev in e.new_events if ev["kind"] == "recenter") <= 2
    assert e.s["base"] * 115 <= 0.65 * CAPITAL


def test_recenter_never_discards_the_new_grids_initial_buy():
    """Achado real de uso (Laboratório de Cenários): _rebuild_grid montava a grelha nova via setup(), que compra a
    mercado o inventário dos degraus acima do preço (gasta "quote", soma a "base") — mas depois sobrescrevia "base"
    só com o pó antigo de antes do recentrar, deitando fora essa compra: o capital "desaparecia" da contabilidade
    numa só vela, sem qualquer perda real de mercado (equity a cair dezenas de % só por recentrar em alta)."""
    e = new_engine()
    path = flat(30) + line(100, 130, 200) + flat(1500, 130)              # sobe bem acima da grelha e fica lá
    prev_equity, drops = None, []
    for c in candles(path):
        e.process_candle(c)
        eq = e.equity(c[4])
        if prev_equity is not None and eq < prev_equity * 0.9:           # queda de mais de 10% numa só vela
            drops.append((c[0], prev_equity, eq))
        prev_equity = eq
    assert any(ev["kind"] == "recenter" for ev in e.new_events)          # confirma que o cenário testou um recentrar
    assert drops == [], f"quedas súbitas de equity num só passo: {drops}"


def test_a_recenter_the_risk_guard_would_refuse_never_crashes_the_step():
    """Achado real de uso: _rebuild_grid chama setup(), que pode levantar GridRefused (ex.: o dinheiro que sobrou
    já não chega para uma grelha segura ao preço novo) — isto propagava sem apanhar e derrubava o passo do bot
    inteiro (500 no painel do Laboratório; no corredor real só era apanhado pelo try/except genérico do runner,
    escondendo o problema em vez de o registar como um evento claro)."""
    e = run(new_engine(), candles(flat(3)))
    old_grid = e.grid
    e.s["quote"], e.s["reserve"] = 2.0, 1.0                               # de propósito pouco para uma grelha nova
    ok = e._rebuild_grid(e.last_ts + MIN, 100.0)                          # não deve levantar GridRefused
    assert ok is False
    assert e.grid is old_grid                                            # grelha antiga mantida, nada a meio
    assert any(ev["kind"] == "guard" and "recusad" in ev["detail"] for ev in e.new_events)


def test_wick_of_4_percent_does_not_trigger_the_stop_loss():
    path = flat(60) + [96] * 3 + flat(60)                                # pavio de -4% durante 3 minutos
    e = run(new_engine(), candles(path))
    assert e.s["stop_events"] == 0 and e.status != STOPPED
    small = run(new_engine(), candles(flat(60) + [97.5] * 3 + flat(60)))  # -2,5%: nada dispara
    assert small.status == RUNNING and not any(ev["kind"] == "pause" for ev in small.new_events)


def test_daily_loss_limit_pauses_the_bot_and_records_the_reason():
    e = run(new_engine(drop_pause_pct=50, daily_loss_pct=1.0), candles(flat(30) + line(100, 95, 60) + flat(30, 95)))
    assert e.status == PAUSED and "limite diário" in e.reason
    assert not any(o["side"] == "buy" for o in e.orders)
    e.command("resume", e.last_ts + MIN)
    assert e.status == RUNNING


def test_user_commands_pause_resume_stop():
    e = run(new_engine(), candles(flat(30)))
    e.command("pause", e.last_ts + MIN)
    assert e.status == PAUSED and not any(o["side"] == "buy" for o in e.orders)
    e.command("resume", e.last_ts + MIN)
    assert e.status == RUNNING and any(o["side"] == "buy" for o in e.orders)
    e.command("stop", e.last_ts + MIN)
    assert e.status == STOPPED and e.s["base"] == 0 and e.orders == []


# ---------- retomar sem duplicar ----------
def test_restart_resumes_exactly_without_duplicating_orders():
    cs = candles(sine(1500, amp=0.05, period=300))
    whole = run(new_engine(), cs)
    part = run(new_engine(), cs[:700])
    saved = json.loads(json.dumps(part.dump()))                           # "o Pi desligou": só o estado guardado
    resumed = Engine(saved)
    run(resumed, cs[700:])
    assert resumed.dump()["orders"] == whole.dump()["orders"]
    assert resumed.s["cycles"] == whole.s["cycles"] and resumed.s["realized"] == pytest.approx(whole.s["realized"])
    assert resumed.equity(cs[-1][4]) == pytest.approx(whole.equity(cs[-1][4]))


# ---------- "desfazer" ao pausar: repor as compras canceladas ao retomar ----------
def test_resume_restores_the_exact_cancelled_buys_when_price_is_unchanged():
    e = run(new_engine(), candles(flat(5)))
    before = sorted((o["slot"], o["side"], o["price"], o["qty"]) for o in e.orders if o["side"] == "buy")
    assert before                                                          # há mesmo compras pendentes para cancelar
    e.command("pause", e.last_ts + MIN)
    assert not any(o["side"] == "buy" for o in e.orders)
    e.command("resume", e.last_ts + MIN, close=100.0)
    after = sorted((o["slot"], o["side"], o["price"], o["qty"]) for o in e.orders if o["side"] == "buy")
    assert after == before                                                 # exatamente as mesmas compras, nos mesmos degraus e preços
    assert "todas repostas" in e.new_events[-1]["detail"]


def test_resume_warns_instead_of_silently_skipping_a_buy_the_price_already_passed():
    e = run(new_engine(), candles(flat(5)))                                # compras pendentes em 92 e 94
    slots_before = sorted(o["slot"] for o in e.orders if o["side"] == "buy")
    assert len(slots_before) == 2
    e.command("pause", e.last_ts + MIN)
    e.command("resume", e.last_ts + MIN, close=93.5)                       # preço caiu: o degrau 94 já foi "passado"
    restored = sorted(o["slot"] for o in e.orders if o["side"] == "buy")
    missing = [s for s in slots_before if s not in restored]
    assert missing and len(missing) < len(slots_before)                    # repôs o que ainda faz sentido, não tudo
    detail = e.new_events[-1]["detail"]
    assert "não foram repostas" in detail and str(len(missing)) in detail and "preço já passou" in detail


def test_inventory_limit_stops_buying_before_65_percent_of_capital():
    path = flat(30) + line(100, 93, 300)
    e = run(new_engine(drop_pause_pct=50), candles(path))                 # limite de inventário por defeito: 65%
    assert e.s["base"] * 93 <= 0.65 * CAPITAL + 1e-6
    assert e.s["consec_buys"] < 3 or e.s["buys_blocked"]
