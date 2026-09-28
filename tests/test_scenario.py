"""Laboratório de Cenários: gerador de velas sintéticas, o motor real por cima delas, persistência e rotas."""
import re

import pytest

from app import create_app, db, market, scenario
from app.engine import RUNNING, STOPPED
from test_grid import RULES

PAIR = "ABCUSDT"


def ticker(last="100"):
    return {"symbol": PAIR, "lastPrice": last, "highPrice": "103", "lowPrice": "98", "bidPrice": "99.99",
            "askPrice": "100.01", "quoteVolume": "90000000", "priceChangePercent": "1"}


class FakeReader:
    def __init__(self):
        self.tickers = {PAIR: ticker()}

    def tickers_all(self):
        return self.tickers

    def ticker24(self, symbol):
        return ticker()

    def symbol_rules(self, symbol):
        return {**RULES, "status": "TRADING"}


# ---------- gerador de velas ----------
def test_same_seed_and_script_always_produces_the_same_candles():
    script = [{"tipo": "alta", "forca": "moderada", "custom": None, "dur": 2, "unidade": "horas"}]
    c1, b1 = scenario.generate_candles(script, "42", 100.0, 1_700_000_000_000)
    c2, b2 = scenario.generate_candles(script, "42", 100.0, 1_700_000_000_000)
    assert c1 == c2 and b1 == b2
    assert len(c1) == 120                                              # 2 horas = 120 velas de 1 minuto


def test_different_seeds_diverge():
    script = [{"tipo": "lateral", "forca": "moderada", "custom": None, "dur": 1, "unidade": "horas"}]
    c1, _ = scenario.generate_candles(script, "1", 100.0, 0)
    c2, _ = scenario.generate_candles(script, "2", 100.0, 0)
    assert c1 != c2


def test_alta_drifts_up_and_baixa_drifts_down_on_average():
    up_script = [{"tipo": "alta", "forca": "forte", "custom": None, "dur": 3, "unidade": "dias"}]
    down_script = [{"tipo": "baixa", "forca": "forte", "custom": None, "dur": 3, "unidade": "dias"}]
    up, _ = scenario.generate_candles(up_script, "7", 100.0, 0)
    down, _ = scenario.generate_candles(down_script, "7", 100.0, 0)
    assert up[-1][4] > 100.0 * 1.05                                    # deriva forte visível em 3 dias
    assert down[-1][4] < 100.0 * 0.95


def test_lateral_has_no_net_drift_bias_from_a_custom_override():
    script = [{"tipo": "lateral", "forca": None, "custom": 50.0, "dur": 1, "unidade": "dias"}]  # custom ignorado: dir=0
    candles, _ = scenario.generate_candles(script, "9", 100.0, 0)
    assert 80 < candles[-1][4] < 120                                   # sem deriva a sério, só ruído


def test_bands_cover_each_phase_in_order():
    script = [{"tipo": "alta", "forca": "suave", "custom": None, "dur": 30, "unidade": "minutos"},
              {"tipo": "baixa", "forca": "suave", "custom": None, "dur": 30, "unidade": "minutos"}]
    candles, bands = scenario.generate_candles(script, "3", 100.0, 1_000_000)
    assert [b["tipo"] for b in bands] == ["alta", "baixa"]
    assert bands[0]["start_ts"] == 1_000_000 and bands[0]["end_ts"] == bands[1]["start_ts"]
    assert bands[1]["end_ts"] == candles[-1][0] + 60_000


# ---------- motor real por cima das velas ----------
def test_run_engine_produces_a_result_with_equity_and_final_pct():
    script = [{"tipo": "lateral", "forca": "moderada", "custom": None, "dur": 6, "unidade": "horas"}]
    candles, _ = scenario.generate_candles(script, "5", 100.0, 0)
    result = scenario.run_engine(PAIR, 100.0, RULES, {}, candles)
    assert result["status"] in (RUNNING, STOPPED, "paused")
    assert result["equity"] and result["equity"][0]["ts"] >= candles[0][0]
    assert isinstance(result["final_pct"], float)
    assert len(result["candles"]) == len(candles)


def test_a_strong_crash_can_trigger_a_protection():
    script = [{"tipo": "baixa", "forca": "forte", "custom": 80.0, "dur": 6, "unidade": "horas"}]
    candles, _ = scenario.generate_candles(script, "13", 100.0, 0)
    result = scenario.run_engine(PAIR, 100.0, RULES, {}, candles)
    assert result["protections"] or result["status"] != RUNNING          # queda de 80%/dia dispara alguma proteção


# ---------- persistência ----------
def test_save_get_list_delete_round_trip(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    conn.executescript(db.SCHEMA)
    scenario.init(conn)
    script = [{"tipo": "alta", "forca": "suave", "custom": None, "dur": 1, "unidade": "horas"}]
    candles, bands = scenario.generate_candles(script, "1", 100.0, 0)
    result = scenario.run_engine(PAIR, 100.0, RULES, {}, candles)
    run_id = scenario.save(conn, "ensaio 1", PAIR, 100.0, RULES, {}, script, "1", bands, result)
    got = scenario.get(conn, run_id)
    assert got["name"] == "ensaio 1" and got["pair"] == PAIR and got["seed"] == "1"
    assert got["result"]["final_pct"] == result["final_pct"]
    assert [r["id"] for r in scenario.list_runs(conn)] == [run_id]
    scenario.delete(conn, run_id)
    assert scenario.get(conn, run_id) is None
    assert scenario.list_runs(conn) == []


# ---------- rotas ----------
@pytest.fixture
def env(tmp_path):
    market.clear_cache()
    app = create_app({"DATA_DIR": str(tmp_path / "data"), "KEY_FILE": str(tmp_path / "k.json"),
                      "MARKET_FETCH": lambda s: {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "EURUSDT": 1.1},
                      "READER_FACTORY": lambda k, s: FakeReader(), "TESTING": True})
    c = app.test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/setup").get_data(as_text=True)).group(1)
    c.post("/setup", data={"csrf": tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    return c, app


def csrf(c, url):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def test_running_a_scenario_from_the_form_creates_and_shows_a_run(env):
    c, app = env
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo": "alta", "phase_forca": "moderada", "phase_custom": "", "phase_dur": "1", "phase_unidade": "dias",
            "seed": "99", "name": "o meu ensaio"}
    r = c.post("/laboratorio", data=form)
    assert r.status_code == 302 and "ver=" in r.headers["Location"]
    html = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "o meu ensaio" in html and "seed 99" in html

    conn = db.connect(app.config["DB_PATH"])
    assert len(scenario.list_runs(conn)) == 1


def test_running_with_no_valid_phase_is_refused(env):
    c, app = env
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo": "", "phase_forca": "moderada", "phase_custom": "", "phase_dur": "", "phase_unidade": "dias"}
    r = c.post("/laboratorio", data=form, follow_redirects=True)
    assert "Adiciona pelo menos uma fase válida" in r.get_data(as_text=True)
    conn = db.connect(app.config["DB_PATH"])
    assert scenario.list_runs(conn) == []


def test_deleting_a_saved_run(env):
    c, app = env
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo": "lateral", "phase_forca": "suave", "phase_custom": "", "phase_dur": "1", "phase_unidade": "horas",
            "seed": "1"}
    c.post("/laboratorio", data=form)
    conn = db.connect(app.config["DB_PATH"])
    run_id = scenario.list_runs(conn)[0]["id"]
    c.post(f"/laboratorio/{run_id}/apagar", data={"csrf": csrf(c, "/laboratorio")})
    assert scenario.list_runs(conn) == []


def test_a_scenario_run_never_creates_a_real_bot(env):
    c, app = env
    from app import botstore
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo": "alta", "phase_forca": "suave", "phase_custom": "", "phase_dur": "1", "phase_unidade": "horas",
            "seed": "1"}
    c.post("/laboratorio", data=form)
    conn = db.connect(app.config["DB_PATH"])
    assert botstore.all_bots(conn) == []
