"""Portão para a fase de dinheiro real (docs/DESIGN.md): 30-60 dias de Testnet, 100+ ciclos, e bater "comprar e
manter". Só números reais a partir do estado gravado dos bots da Testnet — nunca uma estimativa."""
import pytest

from app import botstore, db, readiness

RULES = {"tick": 0.01, "step": 0.001, "min_notional": 5.0}
DAY = 86_400_000


def fresh_conn(tmp_path):
    conn = db.connect(str(tmp_path / "r.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    return conn


def make_bot(conn, pair="BTCUSDT", capital=100.0, price=100.0, ts=0, mode="testnet"):
    bid = botstore.create(conn, pair, capital, RULES, {}, mode=mode)
    eng = botstore.load_engine(conn, bid)
    eng.setup(ts, price)
    botstore.save_engine(conn, eng)
    return bid


def tune(conn, bid, **overrides):
    eng = botstore.load_engine(conn, bid)
    eng.s.update(overrides)
    botstore.save_engine(conn, eng)


def test_no_testnet_bots_means_nothing_is_ready(tmp_path):
    conn = fresh_conn(tmp_path)
    g = readiness.evaluate(conn, now_ms=10 * DAY)
    assert g["days"] == 0.0 and g["cycles"] == 0 and g["bots"] == [] and g["ready"] is False
    assert g["days_ok"] is False and g["cycles_ok"] is False and g["beats_hold"] is None


def test_simulation_bots_never_count_towards_the_gate(tmp_path):
    conn = fresh_conn(tmp_path)
    make_bot(conn, mode="sim", ts=0)
    g = readiness.evaluate(conn, now_ms=100 * DAY)
    assert g["bots"] == [] and g["ready"] is False


def test_not_enough_days_or_cycles_is_not_ready(tmp_path):
    conn = fresh_conn(tmp_path)
    bid = make_bot(conn, ts=0, price=100.0)
    tune(conn, bid, cycles=5)
    g = readiness.evaluate(conn, now_ms=5 * DAY)
    assert g["days"] == pytest.approx(5.0) and g["days_ok"] is False
    assert g["cycles"] == 5 and g["cycles_ok"] is False
    assert g["ready"] is False


def test_enough_days_and_cycles_but_losing_to_hold_is_not_ready(tmp_path):
    conn = fresh_conn(tmp_path)
    bid = make_bot(conn, ts=0, price=100.0, capital=100.0)
    # o preço subiu 100% mas o bot só ganhou 1%: perde de longe para comprar-e-manter
    tune(conn, bid, cycles=150, quote=101.0, base=0.0, reserve=0.0, last_close=200.0, start_price=100.0)
    g = readiness.evaluate(conn, now_ms=40 * DAY)
    assert g["days_ok"] and g["cycles_ok"]
    assert g["beats_hold"] is False
    assert g["ready"] is False


def test_all_three_criteria_met_is_ready(tmp_path):
    conn = fresh_conn(tmp_path)
    bid = make_bot(conn, ts=0, price=100.0, capital=100.0)
    # o bot ganhou 30% enquanto o preço só subiu 10%: bate comprar-e-manter
    tune(conn, bid, cycles=150, quote=130.0, base=0.0, reserve=0.0, last_close=110.0, start_price=100.0)
    g = readiness.evaluate(conn, now_ms=40 * DAY)
    assert g["days_ok"] and g["cycles_ok"] and g["beats_hold"] is True
    assert g["ready"] is True
    assert g["bots"][0]["pair"] == "BTCUSDT" and g["bots"][0]["vs_hold_pct"] == pytest.approx(20.0)


def test_all_bots_must_beat_hold_and_days_use_the_earliest_start(tmp_path):
    conn = fresh_conn(tmp_path)
    b1 = make_bot(conn, pair="BTCUSDT", ts=0, price=100.0, capital=100.0)
    b2 = make_bot(conn, pair="ETHUSDT", ts=10 * DAY, price=50.0, capital=100.0)
    tune(conn, b1, cycles=60, quote=130.0, base=0.0, reserve=0.0, last_close=110.0, start_price=100.0)
    tune(conn, b2, cycles=60, quote=95.0, base=0.0, reserve=0.0, last_close=60.0, start_price=50.0)  # perde
    g = readiness.evaluate(conn, now_ms=40 * DAY)
    assert g["days"] == pytest.approx(40.0)          # usa o início do bot mais antigo (b1, ts=0), não o de b2
    assert g["cycles"] == 120 and g["cycles_ok"]
    assert g["beats_hold"] is False                   # um só bot a perder chega para recusar o portão inteiro
    assert g["ready"] is False


def test_a_bot_that_never_set_up_a_grid_is_ignored_not_counted_as_zero(tmp_path):
    conn = fresh_conn(tmp_path)
    botstore.create(conn, "BTCUSDT", 100.0, RULES, {}, mode="testnet")   # ainda "pending", sem grelha
    g = readiness.evaluate(conn, now_ms=100 * DAY)
    assert g["bots"] == [] and g["ready"] is False
