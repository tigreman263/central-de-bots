"""Conta real: só infraestrutura e guardas (chave, portão, ativação). Nenhum caminho de código envia ordens
reais ainda — isso fica para uma fase seguinte, deliberadamente fora daqui."""
import re

import pytest

from app import create_app, db, market, readiness
from app.reader import check_trading_permissions
from test_portfolio import GOOD, FakeReader, World

GOOD_TRADING = {"enableReading": True, "ipRestrict": True, "enableSpotAndMarginTrading": True}


# ---------- a regra em si ----------
def test_trading_permission_check_requires_spot_but_rejects_everything_dangerous():
    ok, problems, _ = check_trading_permissions(GOOD_TRADING)
    assert ok and problems == []
    assert not check_trading_permissions({**GOOD_TRADING, "enableSpotAndMarginTrading": False})[0]  # sem isto, não negoceia
    for flag in ("enableWithdrawals", "enableMargin", "enableFutures", "enableInternalTransfer",
                "permitsUniversalTransfer", "enableVanillaOptions"):
        ok, problems, _ = check_trading_permissions({**GOOD_TRADING, flag: True})
        assert not ok, flag
    assert not check_trading_permissions({"enableReading": False, "enableSpotAndMarginTrading": True})[0]


def test_trading_permission_check_warns_without_ip_restriction():
    _, _, warnings = check_trading_permissions({**GOOD_TRADING, "ipRestrict": False})
    assert any("IP" in w for w in warnings)


# ---------- painel ----------
@pytest.fixture
def env(tmp_path):
    market.clear_cache()
    world = World()
    app = create_app({
        "DATA_DIR": str(tmp_path / "data"), "KEY_FILE": str(tmp_path / "keys" / "readonly.json"),
        "REAL_TRADING_KEY_FILE": str(tmp_path / "keys" / "trading.json"),
        "MARKET_FETCH": lambda s: {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "EURUSDT": 1.1},
        "READER_FACTORY": lambda key, secret: FakeReader(world), "TESTING": True,
    })
    c = app.test_client()
    tok = csrf(c, "/setup")
    c.post("/setup", data={"csrf": tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    return c, world, app, tmp_path


def csrf(c, url):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def save_trading_key(c, key="CHAVE-TRADING-xyz", secret="SEGREDO-TRADING-abc"):
    return c.post("/conta-real", data={"csrf": csrf(c, "/conta-real"), "action": "guardar",
                                       "api_key": key, "api_secret": secret}, follow_redirects=True)


def test_page_shows_disabled_by_default_and_the_gate(env):
    c, world, app, tmp = env
    html = c.get("/conta-real").get_data(as_text=True)
    assert "Desativada" in html and "0 / 30" in html and "0 / 100" in html


def test_a_key_with_withdrawal_or_margin_is_refused_and_not_saved(env):
    c, world, app, tmp = env
    for flag in ("enableWithdrawals", "enableMargin", "enableFutures"):
        world.restrictions = {**GOOD_TRADING, flag: True}
        html = save_trading_key(c).get_data(as_text=True)
        assert "Chave recusada" in html, flag
    assert not (tmp / "keys" / "trading.json").exists()


def test_a_key_without_spot_trading_permission_is_refused(env):
    c, world, app, tmp = env
    world.restrictions = GOOD                                  # só leitura, sem negociar: serve para o Portefólio, não para isto
    html = save_trading_key(c).get_data(as_text=True)
    assert "não tem permissão para negociar" in html
    assert not (tmp / "keys" / "trading.json").exists()


def test_a_valid_trading_key_is_saved_separately_from_the_readonly_key(env):
    c, world, app, tmp = env
    world.restrictions = GOOD_TRADING
    save_trading_key(c)
    assert (tmp / "keys" / "trading.json").exists()
    assert not (tmp / "keys" / "readonly.json").exists()        # nunca mistura com a chave só de leitura


def test_removing_the_trading_key_also_turns_off_the_flag_if_it_was_on(env):
    c, world, app, tmp = env
    conn = db.connect(app.config["DB_PATH"])
    db.set_many(conn, {"real_trading_enabled": "1"})
    c.post("/conta-real", data={"csrf": csrf(c, "/conta-real"), "action": "remover"})
    assert db.get_all(db.connect(app.config["DB_PATH"]))["real_trading_enabled"] == "0"


# ---------- ativação: recusa sempre que uma condição falha ----------
def test_activation_refused_without_a_saved_key(env):
    c, world, app, tmp = env
    r = c.post("/conta-real", data={"csrf": csrf(c, "/conta-real"), "action": "ativar",
                                    "confirmar": "ATIVAR DINHEIRO REAL"}, follow_redirects=True)
    assert "Guarda primeiro uma chave" in r.get_data(as_text=True)
    assert db.get_all(db.connect(app.config["DB_PATH"]))["real_trading_enabled"] == "0"


def test_activation_refused_when_the_gate_is_not_met_even_with_a_key_and_right_phrase(env):
    c, world, app, tmp = env
    world.restrictions = GOOD_TRADING
    save_trading_key(c)                                         # chave válida guardada
    r = c.post("/conta-real", data={"csrf": csrf(c, "/conta-real"), "action": "ativar",
                                    "confirmar": "ATIVAR DINHEIRO REAL"}, follow_redirects=True)
    assert "portão ainda não está cumprido" in r.get_data(as_text=True)
    assert db.get_all(db.connect(app.config["DB_PATH"]))["real_trading_enabled"] == "0"


def test_activation_refused_with_the_wrong_confirmation_phrase_even_when_the_gate_is_met(env, monkeypatch):
    c, world, app, tmp = env
    world.restrictions = GOOD_TRADING
    save_trading_key(c)
    monkeypatch.setattr(readiness, "evaluate", lambda conn, now_ms: {"ready": True, "days": 40, "days_target": 30,
                        "days_ok": True, "cycles": 150, "cycles_target": 100, "cycles_ok": True,
                        "beats_hold": True, "bots": []})
    r = c.post("/conta-real", data={"csrf": csrf(c, "/conta-real"), "action": "ativar",
                                    "confirmar": "sim quero"}, follow_redirects=True)
    assert "Escreve exatamente" in r.get_data(as_text=True)
    assert db.get_all(db.connect(app.config["DB_PATH"]))["real_trading_enabled"] == "0"


def test_activation_succeeds_only_when_key_gate_and_phrase_all_line_up(env, monkeypatch):
    c, world, app, tmp = env
    world.restrictions = GOOD_TRADING
    save_trading_key(c)
    monkeypatch.setattr(readiness, "evaluate", lambda conn, now_ms: {"ready": True, "days": 40, "days_target": 30,
                        "days_ok": True, "cycles": 150, "cycles_target": 100, "cycles_ok": True,
                        "beats_hold": True, "bots": []})
    r = c.post("/conta-real", data={"csrf": csrf(c, "/conta-real"), "action": "ativar",
                                    "confirmar": "ATIVAR DINHEIRO REAL"}, follow_redirects=True)
    assert "Ativada" in r.get_data(as_text=True)
    assert db.get_all(db.connect(app.config["DB_PATH"]))["real_trading_enabled"] == "1"


def test_deactivation_always_works(env):
    c, world, app, tmp = env
    conn = db.connect(app.config["DB_PATH"])
    db.set_many(conn, {"real_trading_enabled": "1"})
    c.post("/conta-real", data={"csrf": csrf(c, "/conta-real"), "action": "desativar"})
    assert db.get_all(db.connect(app.config["DB_PATH"]))["real_trading_enabled"] == "0"


def test_real_account_routes_require_login_and_csrf(env):
    c, world, app, tmp = env
    anon = app.test_client()
    assert anon.get("/conta-real").status_code == 302
    assert anon.post("/conta-real").status_code in (302, 400, 403)
    assert c.post("/conta-real", data={"csrf": "errado", "action": "ativar"}).status_code == 400
