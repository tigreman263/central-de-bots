"""Laboratório de Cenários: gerador de velas sintéticas, motor real por cima delas, e a página que os liga."""
import re

import pytest

from app import botstore, create_app, db, market, scenario
from app.engine import PENDING, RUNNING
from test_bots import PAIR, FakeMarket, RULES, ticker

CAPITAL = 200.0


def script(*phases):
    """phases: (tipo, forca, dur, unidade). 'forca' pode ser um preset ou um número (força personalizada)."""
    out = []
    for tipo, forca, dur, unidade in phases:
        custom = None if isinstance(forca, str) else forca
        out.append({"tipo": tipo, "forca": forca if isinstance(forca, str) else "moderada",
                    "custom": custom, "dur": dur, "unidade": unidade})
    return out


# ---------- o gerador em si ----------
def test_same_seed_and_script_always_produce_the_same_candles():
    s = script(("alta", "forte", 2, "horas"), ("baixa", "moderada", 1, "horas"))
    c1, b1 = scenario.generate_candles(s, "semente-fixa", 100.0, 0)
    c2, b2 = scenario.generate_candles(s, "semente-fixa", 100.0, 0)
    assert c1 == c2 and b1 == b2


def test_different_seeds_diverge():
    s = script(("lateral", "moderada", 3, "horas"))
    c1, _ = scenario.generate_candles(s, "semente-a", 100.0, 0)
    c2, _ = scenario.generate_candles(s, "semente-b", 100.0, 0)
    assert c1 != c2


def test_alta_phase_drifts_price_up_on_average():
    s = script(("alta", "forte", 6, "horas"))
    candles, bands = scenario.generate_candles(s, "sobe", 100.0, 0)
    assert candles[-1][4] > candles[0][1]
    assert bands == [(0, 6 * 3_600_000, "alta")]


def test_custom_force_overrides_preset():
    assert scenario.force_pct("suave", 9.9) == 9.9
    assert scenario.force_pct("forte", None) == scenario.PRESETS["forte"]


def test_phase_minutes_converts_every_unit():
    assert scenario.phase_minutes(1, "minutos") == 1
    assert scenario.phase_minutes(1, "horas") == 60
    assert scenario.phase_minutes(1, "dias") == 1440


# ---------- o motor real por cima das velas sintéticas ----------
def test_run_engine_reuses_the_real_grid_engine_and_returns_a_summary():
    s = script(("lateral", "moderada", 4, "horas"))
    candles, _ = scenario.generate_candles(s, "roda", 100.0, 0)
    result = scenario.run_engine(PAIR, CAPITAL, RULES, {}, candles)
    assert result["status"] in (PENDING, RUNNING)
    assert set(result) >= {"events", "fills", "equity", "cycles", "stop_events", "final_pct", "max_dd"}
    assert result["max_dd"] >= 0


def test_a_sharp_crash_can_trigger_the_stop_loss_protection():
    s = script(("baixa", 40.0, 6, "horas"))                        # queda muito forte e sustentada, escrita à mão
    candles, _ = scenario.generate_candles(s, "queda-forte", 100.0, 0)
    result = scenario.run_engine(PAIR, CAPITAL, RULES, {}, candles)
    assert result["stop_events"] >= 1 or result["final_pct"] < 0


# ---------- persistência ----------
def test_save_list_get_and_delete_a_run(tmp_path):
    conn = db.connect(str(tmp_path / "s.db"))
    scenario.init(conn)
    s = script(("alta", "moderada", 1, "horas"))
    candles, bands = scenario.generate_candles(s, "guarda", 100.0, 0)
    result = scenario.run_engine(PAIR, CAPITAL, RULES, {}, candles)
    run_id = scenario.save(conn, "Ensaio 1", PAIR, CAPITAL, RULES, {}, s, "guarda", bands, result)
    rows = scenario.list_runs(conn)
    assert len(rows) == 1 and rows[0]["id"] == run_id and rows[0]["name"] == "Ensaio 1"
    loaded = scenario.get(conn, run_id)
    assert loaded["pair"] == PAIR and loaded["script"] == s and loaded["result"]["final_pct"] == result["final_pct"]
    scenario.delete(conn, run_id)
    assert scenario.list_runs(conn) == []
    assert scenario.get(conn, run_id) is None


# ---------- a página ----------
def tok(c, url):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


@pytest.fixture
def env(tmp_path):
    market.clear_cache()
    fm = FakeMarket([])
    fm.tickers = {PAIR: ticker()}
    app = create_app({"DATA_DIR": str(tmp_path / "data"), "KEY_FILE": str(tmp_path / "k.json"),
                      "MARKET_FETCH": lambda s: {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "EURUSDT": 1.1},
                      "READER_FACTORY": lambda k, s: fm, "TESTING": True})
    c = app.test_client()
    setup_tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/setup").get_data(as_text=True)).group(1)
    c.post("/setup", data={"csrf": setup_tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    return c, fm, app


def base_form(c):
    return {"csrf": tok(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "150",
            "phase_tipo": "alta", "phase_forca": "moderada", "phase_custom": "", "phase_dur": "3", "phase_unidade": "horas",
            "seed": "ensaio-fixo", "name": "Meu ensaio"}


def test_the_page_loads_with_an_empty_history(env):
    c, fm, app = env
    html = c.get("/laboratorio").get_data(as_text=True)
    assert "Laboratório de Cenários" in html
    assert "Ainda não correste nenhum ensaio" in html


def test_running_a_new_config_scenario_creates_and_shows_a_run(env):
    c, fm, app = env
    r = c.post("/laboratorio", data=base_form(c), follow_redirects=True)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Meu ensaio" in html
    assert "ensaio-fixo" in html
    rows = scenario.list_runs(db.connect(app.config["DB_PATH"]))
    assert len(rows) == 1 and rows[0]["pair"] == PAIR


def test_running_from_an_existing_bot_copies_its_configuration(env):
    c, fm, app = env
    conn = db.connect(app.config["DB_PATH"])
    bot_id = botstore.create(conn, PAIR, 321.0, RULES, {}, mode="sim")
    form = {"csrf": tok(c, "/laboratorio"), "bot_src": "existente", "bot_id": str(bot_id),
            "phase_tipo": "lateral", "phase_forca": "suave", "phase_custom": "", "phase_dur": "1", "phase_unidade": "dias",
            "seed": "copia-do-bot", "name": ""}
    r = c.post("/laboratorio", data=form, follow_redirects=True)
    rows = scenario.list_runs(db.connect(app.config["DB_PATH"]))
    assert len(rows) == 1 and rows[0]["capital"] == 321.0


def test_a_scenario_needs_at_least_one_phase(env):
    c, fm, app = env
    form = {**base_form(c), "phase_tipo": "", "phase_forca": "", "phase_custom": "", "phase_dur": "", "phase_unidade": ""}
    r = c.post("/laboratorio", data=form, follow_redirects=True)
    assert "Adiciona pelo menos uma fase válida" in r.get_data(as_text=True)
    assert scenario.list_runs(db.connect(app.config["DB_PATH"])) == []


def test_deleting_a_saved_run_removes_it(env):
    c, fm, app = env
    c.post("/laboratorio", data=base_form(c))
    run_id = scenario.list_runs(db.connect(app.config["DB_PATH"]))[0]["id"]
    c.post(f"/laboratorio/{run_id}/apagar", data={"csrf": tok(c, "/laboratorio")})
    assert scenario.list_runs(db.connect(app.config["DB_PATH"])) == []


def test_a_scenario_run_never_creates_a_real_bot(env):
    """O ensaio nunca pode aparecer na lista de bots nem contar para o portão da conta real."""
    c, fm, app = env
    c.post("/laboratorio", data=base_form(c))
    conn = db.connect(app.config["DB_PATH"])
    assert botstore.all_bots(conn) == []
    assert scenario.list_runs(conn) != []
