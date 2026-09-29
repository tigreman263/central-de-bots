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


def test_default_alta_phase_reliably_trends_up_not_dominated_by_noise():
    """Achado real de uso: com a calibração antiga (ruído demasiado alto face à deriva), uma fase "moderada" de
    1h tinha ~33% de hipótese de terminar em queda mesmo escolhida como "alta" — o guião deixava de significar o
    que o utilizador escolheu. Com a calibração corrigida, a direção escolhida tem de dominar quase sempre para a
    combinação por defeito do formulário (moderada, 1 dia)."""
    script = [{"tipo": "alta", "forca": "moderada", "custom": None, "dur": 1, "unidade": "dias"}]
    negativos = 0
    for seed in range(60):
        candles, _ = scenario.generate_candles(script, str(seed), 100.0, 0)
        if candles[-1][4] < 100.0:
            negativos += 1
    assert negativos == 0, f"{negativos}/60 seeds terminaram em queda numa fase 'alta'"


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


def test_list_runs_sorts_by_result_or_recency(tmp_path):
    conn = db.connect(str(tmp_path / "t2.db"))
    conn.executescript(db.SCHEMA)
    scenario.init(conn)
    ids = {}
    for name, pct in [("subiu", 5.0), ("caiu", -3.0), ("neutro", 0.5)]:
        script = [{"tipo": "alta", "forca": "suave", "custom": None, "dur": 1, "unidade": "horas"}]
        candles, bands = scenario.generate_candles(script, name, 100.0, 0)
        result = scenario.run_engine(PAIR, 100.0, RULES, {}, candles)
        result["final_pct"] = pct                                    # força um valor conhecido para testar a ordem
        ids[name] = scenario.save(conn, name, PAIR, 100.0, RULES, {}, script, name, bands, result)
    melhor = [r["name"] for r in scenario.list_runs(conn, order="melhor")]
    pior = [r["name"] for r in scenario.list_runs(conn, order="pior")]
    recentes = [r["name"] for r in scenario.list_runs(conn, order="recentes")]
    assert melhor == ["subiu", "neutro", "caiu"]
    assert pior == ["caiu", "neutro", "subiu"]
    assert recentes == ["neutro", "caiu", "subiu"]                    # o último gravado primeiro


def test_init_migrates_a_scenario_runs_table_from_before_the_bands_column(tmp_path):
    """Achado real de uso: uma base de dados que já tinha scenario_runs de uma versão anterior (sem "bands")
    ficava presa em CREATE TABLE IF NOT EXISTS (não faz nada) e rebentava com OperationalError ao gravar."""
    conn = db.connect(str(tmp_path / "old.db"))
    conn.executescript(db.SCHEMA)
    conn.executescript("""
        CREATE TABLE scenario_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, pair TEXT NOT NULL, capital REAL NOT NULL,
            rules TEXT NOT NULL, params TEXT NOT NULL, script TEXT NOT NULL,
            seed TEXT NOT NULL, result TEXT NOT NULL, created_ts TEXT NOT NULL
        );
    """)
    scenario.init(conn)
    assert "bands" in {r[1] for r in conn.execute("PRAGMA table_info(scenario_runs)")}
    script = [{"tipo": "alta", "forca": "suave", "custom": None, "dur": 1, "unidade": "horas"}]
    candles, bands = scenario.generate_candles(script, "1", 100.0, 0)
    result = scenario.run_engine(PAIR, 100.0, RULES, {}, candles)
    run_id = scenario.save(conn, "ensaio", PAIR, 100.0, RULES, {}, script, "1", bands, result)   # não rebenta
    assert scenario.get(conn, run_id)["name"] == "ensaio"


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
            "phase_tipo_0": "alta", "phase_forca_0": "moderada", "phase_custom_0": "", "phase_dur_0": "1",
            "phase_unidade_0": "dias", "seed": "99", "name": "o meu ensaio"}
    r = c.post("/laboratorio", data=form)
    assert r.status_code == 302 and "ver=" in r.headers["Location"]
    html = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "o meu ensaio" in html and "seed 99" in html

    conn = db.connect(app.config["DB_PATH"])
    assert len(scenario.list_runs(conn)) == 1


def test_running_with_no_valid_phase_is_refused(env):
    c, app = env
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo_0": "", "phase_forca_0": "moderada", "phase_custom_0": "", "phase_dur_0": "",
            "phase_unidade_0": "dias"}
    r = c.post("/laboratorio", data=form, follow_redirects=True)
    assert "Adiciona pelo menos uma fase válida" in r.get_data(as_text=True)
    conn = db.connect(app.config["DB_PATH"])
    assert scenario.list_runs(conn) == []


def test_deleting_a_saved_run(env):
    c, app = env
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo_0": "lateral", "phase_forca_0": "suave", "phase_custom_0": "", "phase_dur_0": "1",
            "phase_unidade_0": "horas", "seed": "1"}
    c.post("/laboratorio", data=form)
    conn = db.connect(app.config["DB_PATH"])
    run_id = scenario.list_runs(conn)[0]["id"]
    c.post(f"/laboratorio/{run_id}/apagar", data={"csrf": csrf(c, "/laboratorio")})
    assert scenario.list_runs(conn) == []


def test_a_scenario_run_never_creates_a_real_bot(env):
    c, app = env
    from app import botstore
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo_0": "alta", "phase_forca_0": "suave", "phase_custom_0": "", "phase_dur_0": "1",
            "phase_unidade_0": "horas", "seed": "1"}
    c.post("/laboratorio", data=form)
    conn = db.connect(app.config["DB_PATH"])
    assert botstore.all_bots(conn) == []


def test_saved_runs_list_paginates_and_sorts(env):
    c, app = env
    for i in range(17):
        form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
                "phase_tipo_0": "lateral", "phase_forca_0": "suave", "phase_custom_0": "", "phase_dur_0": "1",
                "phase_unidade_0": "horas", "seed": str(i), "name": f"ensaio {i}"}
        c.post("/laboratorio", data=form)
    html = c.get("/laboratorio").get_data(as_text=True)
    assert "página 1 de 2" in html
    html2 = c.get("/laboratorio?pagina=2").get_data(as_text=True)
    assert "página 2 de 2" in html2
    html_melhor = c.get("/laboratorio?ordenar=melhor").get_data(as_text=True)
    assert "Melhor resultado" in html_melhor
    r_bad = c.get("/laboratorio?ordenar=xyz")                          # valor inválido: nunca rebenta, cai no default
    assert r_bad.status_code == 200


def test_risk_overrides_replace_only_the_fields_filled_in(env):
    c, app = env
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo_0": "alta", "phase_forca_0": "forte", "phase_custom_0": "", "phase_dur_0": "1",
            "phase_unidade_0": "dias", "seed": "5", "risk_max_loss_trade_pct": "1.5"}
    r = c.post("/laboratorio", data=form)
    conn = db.connect(app.config["DB_PATH"])
    run_id = scenario.list_runs(conn)[0]["id"]
    got = scenario.get(conn, run_id)
    assert got["params"]["max_loss_trade_pct"] == 1.5                 # o que foi escrito
    assert got["params"]["daily_loss_pct"] == 3.0                     # o resto fica no valor por defeito
    html = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "perda por operação 1.5%" in html


def test_comparing_two_saved_runs_shows_both_side_by_side(env):
    c, app = env
    for seed, name in [("1", "ensaio A"), ("2", "ensaio B")]:
        form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
                "phase_tipo_0": "alta", "phase_forca_0": "forte", "phase_custom_0": "", "phase_dur_0": "1",
                "phase_unidade_0": "dias", "seed": seed, "name": name}
        c.post("/laboratorio", data=form)
    conn = db.connect(app.config["DB_PATH"])
    ids = [r["id"] for r in scenario.list_runs(conn, order="recentes")]
    r = c.get(f"/laboratorio?ver={ids[0]}&vs={ids[1]}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "ensaio A" in html and "ensaio B" in html and "Fechar comparação" in html


def test_comparing_against_yourself_is_ignored(env):
    c, app = env
    form = {"csrf": csrf(c, "/laboratorio"), "bot_src": "novo", "par": PAIR, "capital": "100",
            "phase_tipo_0": "lateral", "phase_forca_0": "suave", "phase_custom_0": "", "phase_dur_0": "1",
            "phase_unidade_0": "horas", "seed": "1"}
    c.post("/laboratorio", data=form)
    conn = db.connect(app.config["DB_PATH"])
    run_id = scenario.list_runs(conn)[0]["id"]
    r = c.get(f"/laboratorio?ver={run_id}&vs={run_id}")
    assert r.status_code == 200
    assert "Fechar comparação" not in r.get_data(as_text=True)
