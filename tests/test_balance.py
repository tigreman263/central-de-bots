"""Gestor de saldo (reserva/trading/caixa): avisa quando o dinheiro em bots ativos passa a fatia "Trading"
configurada. Nunca bloqueia nem move dinheiro — só compara e devolve o resultado, para o painel avisar."""
import re

import pytest

from app import balance, db
from test_bots import env, make_bot  # noqa: F401


def snapshot(total_usdt):
    return {"total_usdt": total_usdt, "holdings": []}


# ---------- a função pura ----------
def test_over_the_limit_when_active_capital_exceeds_the_trading_slice():
    u = balance.trading_usage(90.0, snapshot(100.0), split_trading_pct=10.0)   # limite = 10 USDT
    assert u["over"] is True and u["limit_usdt"] == pytest.approx(10.0) and u["over_usdt"] == pytest.approx(80.0)


def test_not_over_when_active_capital_fits_the_trading_slice():
    u = balance.trading_usage(5.0, snapshot(100.0), split_trading_pct=10.0)
    assert u["over"] is False and u["over_usdt"] == 0.0


def test_never_invents_a_limit_without_a_real_portfolio():
    u = balance.trading_usage(9999.0, None, split_trading_pct=10.0)
    assert u["limit_usdt"] is None and u["over_usdt"] is None and u["over"] is False


def test_exactly_at_the_limit_is_not_over():
    u = balance.trading_usage(10.0, snapshot(100.0), split_trading_pct=10.0)
    assert u["over"] is False and u["over_usdt"] == 0.0


# ---------- painel ----------
def test_bots_page_warns_when_active_bots_pass_the_trading_slice(env):
    c, fm, app = env
    make_bot(app, capital=90.0)
    conn = db.connect(app.config["DB_PATH"])
    db.set_many(conn, {"split_trading": "10"})
    db.set_many(conn, {"portfolio_snapshot": '{"total_usdt": 100.0, "holdings": []}'})
    html = c.get("/bots").get_data(as_text=True)
    assert "mais do que a fatia" in html and "90.00 USDT" in html


def test_bots_page_says_nothing_when_within_the_trading_slice(env):
    c, fm, app = env
    make_bot(app, capital=5.0)
    conn = db.connect(app.config["DB_PATH"])
    db.set_many(conn, {"split_trading": "10"})
    db.set_many(conn, {"portfolio_snapshot": '{"total_usdt": 100.0, "holdings": []}'})
    html = c.get("/bots").get_data(as_text=True)
    assert "mais do que a fatia" not in html


def test_bots_page_never_warns_without_a_real_portfolio_connected(env):
    c, fm, app = env
    make_bot(app, capital=99999.0)                                         # exagerado de propósito
    html = c.get("/bots").get_data(as_text=True)                           # sem snapshot ligado
    assert "mais do que a fatia" not in html


def test_a_stopped_bots_capital_does_not_count_as_active(env):
    c, fm, app = env
    bid = make_bot(app, capital=90.0)
    conn = db.connect(app.config["DB_PATH"])
    conn.execute("UPDATE bots SET status = 'stopped' WHERE id = ?", (bid,))
    conn.commit()
    db.set_many(conn, {"split_trading": "10"})
    db.set_many(conn, {"portfolio_snapshot": '{"total_usdt": 100.0, "holdings": []}'})
    html = c.get("/bots").get_data(as_text=True)
    assert "mais do que a fatia" not in html
