"""Painel de validação: a checklist nunca inventa um resultado. Estes testes NUNCA deixam `_run_suite` correr de
verdade (isso lançaria a suite completa dentro da própria suite); tapam-lo sempre com resultados fabricados."""
import re
import time

import pytest

from app import db, validation
from app.validation import (FALHOU, NA, NAO_VALIDADO, PARCIAL, PASSOU, CHECKLIST, STATES, _file_item_result,
                            _pytest_item_result)


def all_tests():
    return sorted({t for item in CHECKLIST if item["how"] == "pytest" for t in item["tests"]})


# ---------- a checklist em si: integridade estrutural ----------
def test_every_item_has_the_required_shape_and_a_unique_id():
    ids = [it["id"] for it in CHECKLIST]
    assert len(ids) == len(set(ids))                                        # sem ids repetidos
    for it in CHECKLIST:
        assert it["how"] in ("pytest", "file", "manual", "suite"), it["id"]
        assert it["cat"] and it["name"] and it["requirement"] and it["impact"]
        assert isinstance(it["critical"], bool)
        if it["how"] == "pytest":
            assert it["tests"] and all("::" in t for t in it["tests"])
        elif it["how"] == "file":
            assert it["paths"]
        elif it["how"] == "manual":
            assert it["status"] in STATES and it["evidence"]


def test_the_checklist_never_names_a_forbidden_literal_outside_trader(tmp_path):
    """A mesma regra de isolamento que os testes de rede já impõem: nenhum ficheiro fora de trader.py pode conter
    o domínio da Testnet ou o literal "DELETE" — nem em texto descritivo desta checklist."""
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "app" / "validation.py").read_text(encoding="utf-8")
    assert '"DELETE"' not in text and "/api/v3/order" not in text and "testnet.binance.vision" not in text


def test_referenced_pytest_node_ids_exist_as_real_test_functions():
    """Cada teste que a checklist promete correr tem mesmo de existir (senão fica NÃO_VALIDADO, nunca PASSOU)."""
    import importlib
    import inspect
    missing = []
    for nodeid in all_tests():
        path, name = nodeid.split("::")
        name = name.split("[")[0]                                           # tira o sufixo do parametrize, ex.: "[0]"
        mod_name = path.split("/")[-1][:-3]
        mod = importlib.import_module(mod_name)
        if not hasattr(mod, name) or not inspect.isfunction(getattr(mod, name)):
            missing.append(nodeid)
    assert not missing, missing


def test_critical_blockers_include_the_hardest_real_requirements():
    """Os requisitos que os relatórios de todas as fases sempre trataram como inegociáveis estão marcados críticos."""
    critical_ids = {it["id"] for it in CHECKLIST if it["critical"]}
    for must in ("tr-retoma", "pt-stop1", "pt-stop2", "pt-stop4", "rec-registo-idempotente", "seg-isolamento", "pi-real"):
        assert must in critical_ids, must


# ---------- avaliação pura de um item (sem correr nada) ----------
def test_pytest_item_result_all_passed_is_passou():
    item = {"tests": ["tests/x.py::a", "tests/x.py::b"]}
    outcomes = {"tests/x.py::a": "PASSED", "tests/x.py::b": "PASSED"}
    status, found, error = _pytest_item_result(item, outcomes)
    assert status == PASSOU and error is None


def test_pytest_item_result_any_failed_is_falhou_and_names_it():
    item = {"tests": ["tests/x.py::a", "tests/x.py::b"]}
    outcomes = {"tests/x.py::a": "PASSED", "tests/x.py::b": "FAILED"}
    status, found, error = _pytest_item_result(item, outcomes)
    assert status == FALHOU and "tests/x.py::b" in error


def test_pytest_item_result_missing_outcome_is_nao_validado_never_passou():
    item = {"tests": ["tests/x.py::a"]}
    status, found, error = _pytest_item_result(item, {})
    assert status == NAO_VALIDADO and "não encontrei" not in "".join(str(error)).lower() or True
    assert status == NAO_VALIDADO


def test_file_item_result_checks_real_existence(tmp_path):
    real = tmp_path / "existe.txt"
    real.write_text("x")
    status, obtained, error = _file_item_result({"paths": [real]})
    assert status == PASSOU and error is None
    status, obtained, error = _file_item_result({"paths": [real, tmp_path / "nao-existe.txt"]})
    assert status == FALHOU and "nao-existe.txt" in error


# ---------- run() com a suite "tapada" (nunca corre pytest de verdade aqui) ----------
def fake_run_suite(outcomes, tail="", dur=1.0):
    def _fake(node_ids=None, on_result=None):
        return tail, outcomes, dur
    return _fake


def all_passed_outcomes():
    return {t: "PASSED" for t in all_tests()}


def test_run_with_everything_passing_is_at_most_nao_validado_never_pronto_by_accident(monkeypatch):
    """Mesmo com TODOS os testes automáticos a passar, os itens manuais críticos (Pi real, etc.) continuam
    NÃO_VALIDADO — por isso o veredicto nunca pode saltar sozinho para PRONTO."""
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    rec = validation.run()
    assert rec["verdict"] == "nao_validado"
    assert rec["counts"][FALHOU] == 0 and rec["counts"][PARCIAL] == 0
    assert "pi-real" in rec["blockers"] and "pi-testnet-real" in rec["blockers"]


def test_run_with_a_critical_test_failing_is_nao_pronto(monkeypatch):
    outcomes = all_passed_outcomes()
    a_critical_test = next(t for it in CHECKLIST if it["critical"] and it["how"] == "pytest" for t in it["tests"])
    outcomes[a_critical_test] = "FAILED"
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(outcomes))
    rec = validation.run()
    assert rec["verdict"] == "nao_pronto"
    failing_items = [r for r in rec["results"] if r["status"] == FALHOU]
    assert any(a_critical_test in (r["evidence"] or "") for r in failing_items)


def test_run_never_marks_an_item_passou_when_its_tests_did_not_run(monkeypatch):
    """Se a suite não chegou a correr um teste (ex.: crash a meio), esse item fica NÃO_VALIDADO, nunca PASSOU."""
    outcomes = all_passed_outcomes()
    some_test = next(iter(outcomes))
    del outcomes[some_test]
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(outcomes))
    rec = validation.run()
    affected = [r for r in rec["results"] if r["how"] == "pytest" and some_test in r["evidence"]]
    assert affected and all(r["status"] == NAO_VALIDADO for r in affected)


def test_suite_summary_item_reflects_the_real_pass_fail_counts(monkeypatch):
    outcomes = {"tests/x.py::a": "PASSED", "tests/x.py::b": "FAILED"}
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(outcomes))
    rec = validation.run()
    suite_item = next(r for r in rec["results"] if r["id"] == "te-suite-completa")
    assert suite_item["status"] == FALHOU and "1 passed" in suite_item["obtained"] and "1 failed" in suite_item["obtained"]


def test_manual_items_never_become_passou_or_falhou_by_running_the_suite(monkeypatch):
    """Itens 'manual' são fixos por definição: correr a suite não os transforma em PASSOU nem em FALHOU."""
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    rec = validation.run()
    for it in CHECKLIST:
        if it["how"] == "manual":
            got = next(r for r in rec["results"] if r["id"] == it["id"])
            assert got["status"] == it["status"]


def test_view_categories_sum_to_the_total_and_blockers_are_only_critical_non_passing(monkeypatch):
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    conn = _fresh_conn()
    validation.run(persist_conn=conn)
    v = validation.view(conn)
    assert sum(c["total"] for c in v["categories"]) == v["run"]["total_items"]
    for b in v["blockers"]:
        assert b["critical"] and b["status"] != PASSOU and b["status"] != NA


def _fresh_conn():
    import tempfile
    d = tempfile.mkdtemp()
    conn = db.connect(d + r"\v.db")
    conn.executescript(db.SCHEMA)
    return conn


# ---------- persistência ----------
def test_save_load_last_and_history_round_trip(monkeypatch):
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    conn = _fresh_conn()
    rec = validation.run(persist_conn=conn)
    loaded = validation.load_last(conn)
    assert loaded["verdict"] == rec["verdict"] and loaded["total_items"] == rec["total_items"]
    hist = validation.history(conn)
    assert len(hist) == 1 and hist[0]["verdict"] == rec["verdict"]


def test_history_keeps_only_the_last_n_runs(monkeypatch):
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    conn = _fresh_conn()
    for i in range(validation.HISTORY_KEEP + 5):
        validation.save(conn, {**validation.run(), "ts": time.time() + i})
    assert len(validation.history(conn, limit=1000)) == validation.HISTORY_KEEP


def test_load_last_with_nothing_saved_yet_returns_none():
    conn = _fresh_conn()
    assert validation.load_last(conn) is None


# ---------- painel (rotas) ----------
@pytest.fixture
def panel_env(monkeypatch, tmp_path):
    from app import create_app
    from test_bots import FakeMarket
    from test_grid import candles, flat
    fm = FakeMarket(candles(flat(5)))
    app = create_app({"DATA_DIR": str(tmp_path / "data"), "KEY_FILE": str(tmp_path / "k.json"),
                      "MARKET_FETCH": lambda s: {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "EURUSDT": 1.1},
                      "READER_FACTORY": lambda k, s: fm, "TESTING": True})
    c = app.test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/setup").get_data(as_text=True)).group(1)
    c.post("/setup", data={"csrf": tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    return c, app


def csrf(c, url):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def run_and_wait(c, modo="tudo", timeout=5.0):
    """A rota /validacao/correr só arranca a validação num fio à parte (para a janela flutuante do painel
    sondar o progresso); os testes esperam aqui que essa validação termine antes de olhar para a página.
    Envia o mesmo cabeçalho que o JavaScript real manda, para receber JSON (não a alternativa sem JS)."""
    r = c.post("/validacao/correr", data={"csrf": csrf(c, "/validacao"), "modo": modo},
              headers={"X-Requested-With": "XMLHttpRequest"})
    t0 = time.time()
    while validation.is_running():
        if time.time() - t0 > timeout:
            raise TimeoutError("validação em segundo plano não terminou a tempo")
        time.sleep(0.02)
    return r


def test_panel_shows_the_run_button_before_any_validation_exists(panel_env):
    c, app = panel_env
    html = c.get("/validacao").get_data(as_text=True)
    assert "Executar validação agora" in html and "PRONTO" not in html


def test_panel_runs_and_shows_the_dashboard_with_real_looking_numbers(panel_env, monkeypatch):
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    r = run_and_wait(c)
    assert r.get_json()["started"] is True
    html = c.get("/validacao").get_data(as_text=True)
    assert "NÃO VALIDADO" in html.upper() and "Executar validação" in html
    assert "❌ BLOQUEADORES" in html or "ZERO BLOQUEADORES" in html
    for icon in ("✅", "❓"):
        assert icon in html
    assert "Raspberry Pi" in html and "PARAR TUDO" in html and "Recovery" in html


def test_panel_never_shows_pronto_when_there_is_a_manual_unvalidated_critical_item(panel_env, monkeypatch):
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    run_and_wait(c)
    html = c.get("/validacao").get_data(as_text=True)
    assert "PRONTO PARA AUDITORIA FINAL" not in html                        # há bloqueadores manuais (Pi real, etc.)


def test_panel_shows_nao_pronto_when_a_critical_test_fails(panel_env, monkeypatch):
    c, app = panel_env
    outcomes = all_passed_outcomes()
    critical_test = next(t for it in CHECKLIST if it["critical"] and it["how"] == "pytest" for t in it["tests"])
    outcomes[critical_test] = "FAILED"
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(outcomes))
    run_and_wait(c)
    html = c.get("/validacao").get_data(as_text=True)
    assert "NÃO PRONTO" in html.upper() and "❌ BLOQUEADORES" in html


def test_panel_item_detail_shows_everything_the_spec_asked_for(panel_env, monkeypatch):
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    run_and_wait(c)
    html = c.get("/validacao").get_data(as_text=True)
    for label in ("Requisito:", "Como se verificou:", "Evidência", "Obtido:", "Impacto", "Bloqueia a auditoria final:",
                 "Duração desta verificação:"):
        assert label in html, label


def test_running_again_updates_the_dashboard_and_keeps_history(panel_env, monkeypatch):
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    run_and_wait(c)
    outcomes2 = all_passed_outcomes()
    a_test = next(t for it in CHECKLIST if it["how"] == "pytest" for t in it["tests"])
    outcomes2[a_test] = "FAILED"
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(outcomes2))
    run_and_wait(c)
    html = c.get("/validacao").get_data(as_text=True)
    assert "NÃO PRONTO" in html.upper()                                    # o painel reflete a validação mais recente
    assert "Histórico de validações" in html


def test_validation_route_requires_login(panel_env):
    c, app = panel_env
    anon = app.test_client()
    assert anon.get("/validacao").status_code == 302
    assert anon.post("/validacao/correr").status_code in (302, 400, 403)


def test_validation_run_route_requires_csrf(panel_env, monkeypatch):
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    r = c.post("/validacao/correr", data={"csrf": "errado"})
    assert r.status_code in (400, 403)
    assert validation.load_last(db.connect(app.config["DB_PATH"])) is None


# ---------- reverificação incremental: só o que ainda não está PASSOU (mais a opção de forçar tudo) ----------
def test_incremental_mode_without_a_previous_run_verifies_everything(monkeypatch):
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    conn = _fresh_conn()
    rec = validation.run(persist_conn=conn, mode="pendentes")
    assert rec["total_items"] == len(CHECKLIST)
    assert all(r["obtained"] != "anterior" for r in rec["results"])   # nada herdado: é a primeira vez


def test_incremental_mode_leaves_a_previously_passing_item_untouched(monkeypatch):
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    conn = _fresh_conn()
    first = validation.run(persist_conn=conn, mode="tudo")
    a_test = next(t for it in CHECKLIST if it["how"] == "pytest" and it["id"] != "te-suite-completa" for t in it["tests"])
    item_id = next(it["id"] for it in CHECKLIST if a_test in it.get("tests", []))
    assert next(r for r in first["results"] if r["id"] == item_id)["status"] == PASSOU

    outcomes2 = all_passed_outcomes()
    outcomes2[a_test] = "FAILED"                      # se isto fosse reverificado, passava a FALHOU
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(outcomes2))
    second = validation.run(persist_conn=conn, mode="pendentes")
    result = next(r for r in second["results"] if r["id"] == item_id)
    assert result["status"] == PASSOU                 # continua PASSOU: não foi reverificado por defeito
    assert result["duration_s"] == next(r for r in first["results"] if r["id"] == item_id)["duration_s"]


def test_incremental_mode_uses_a_targeted_pytest_call_not_the_whole_suite(monkeypatch):
    conn = _fresh_conn()
    target_item = next(it for it in CHECKLIST if it["how"] == "pytest" and it["id"] != "te-suite-completa")
    fabricated = []
    for it in CHECKLIST:
        if it["id"] == target_item["id"]:
            status = FALHOU
        elif it["how"] == "manual":
            status = it["status"]
        else:
            status = PASSOU
        fabricated.append({"id": it["id"], "cat": it["cat"], "name": it["name"], "requirement": it["requirement"],
                            "how": it["how"], "status": status, "obtained": "anterior", "evidence": "anterior",
                            "error": None, "critical": it["critical"], "impact": it["impact"], "duration_s": 0.1})
    counts = {s: sum(1 for r in fabricated if r["status"] == s) for s in STATES}
    record = {"ts": time.time(), "duration_s": 1.0, "suite_duration_s": 1.0, "suite_total": 500,
              "suite_passed": 499, "suite_failed": 0, "suite_tail": "", "total_items": len(fabricated),
              "counts": counts, "verdict": "nao_pronto", "blockers": [target_item["id"]], "results": fabricated,
              "commit": "abc123"}
    validation.save(conn, record)

    calls = []
    def spy(node_ids=None, on_result=None):
        calls.append(node_ids)
        return "", {t: "PASSED" for t in target_item["tests"]}, 0.5
    monkeypatch.setattr(validation, "_run_suite", spy)
    result = validation.run(persist_conn=conn, mode="pendentes")
    assert len(calls) == 1 and calls[0] is not None
    assert set(calls[0]) == set(target_item["tests"])   # só correu os testes deste item, não "tests/" inteira
    fixed = next(r for r in result["results"] if r["id"] == target_item["id"])
    assert fixed["status"] == PASSOU


def test_forced_mode_reverifies_even_items_that_already_passed(monkeypatch):
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    conn = _fresh_conn()
    validation.run(persist_conn=conn, mode="tudo")

    calls = []
    def spy(node_ids=None, on_result=None):
        calls.append(node_ids)
        return fake_run_suite(all_passed_outcomes())()
    monkeypatch.setattr(validation, "_run_suite", spy)
    second = validation.run(persist_conn=conn, mode="tudo")
    assert calls and calls[0] is None                   # "tudo" corre sempre a suite completa, mesmo já tudo a passar
    assert all(r["obtained"] != "anterior" for r in second["results"])


# ---------- progresso ao vivo ----------
def test_streamed_results_mark_an_item_done_as_soon_as_its_own_tests_finish_not_only_at_the_end(monkeypatch):
    conn = _fresh_conn()
    item_a = next(it for it in CHECKLIST if it["how"] == "pytest" and it["id"] != "te-suite-completa")
    item_b = next(it for it in CHECKLIST
                  if it["how"] == "pytest" and it["id"] not in ("te-suite-completa", item_a["id"]))

    def fake_streaming(node_ids=None, on_result=None):
        outcomes = {}
        for t in item_a["tests"] + item_b["tests"]:
            outcomes[t] = "PASSED"
            if on_result:
                on_result(t, "PASSED")
        return "", outcomes, 0.2
    monkeypatch.setattr(validation, "_run_suite", fake_streaming)
    validation.run(persist_conn=conn, mode="tudo")
    snap = validation.progress_snapshot()
    ids_done = [d["id"] for d in snap["done"]]
    assert ids_done.count(item_a["id"]) == 1        # reportado ao vivo; nunca duplicado no ciclo final
    assert ids_done.count(item_b["id"]) == 1
    assert all(d["status"] == PASSOU for d in snap["done"] if d["id"] in (item_a["id"], item_b["id"]))



def test_progress_snapshot_reports_every_item_checked(monkeypatch):
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    conn = _fresh_conn()
    validation.run(persist_conn=conn, mode="tudo")
    snap = validation.progress_snapshot()
    assert snap["running"] is False
    assert snap["total"] == len(CHECKLIST)
    assert len(snap["done"]) == len(CHECKLIST)
    assert all("status" in d and "name" in d for d in snap["done"])


def test_begin_and_mark_idle_control_is_running():
    validation.mark_idle()
    assert validation.is_running() is False
    validation.begin()
    assert validation.is_running() is True
    validation.mark_idle()
    assert validation.is_running() is False


def test_route_refuses_a_second_run_while_one_is_in_progress(panel_env):
    c, app = panel_env
    validation.begin()
    try:
        r = c.post("/validacao/correr", data={"csrf": csrf(c, "/validacao"), "modo": "tudo"},
                  headers={"X-Requested-With": "XMLHttpRequest"})
        assert r.status_code == 409
    finally:
        validation.mark_idle()


def test_route_refuses_a_second_run_while_in_progress_without_js_too(panel_env):
    c, app = panel_env
    validation.begin()
    try:
        r = c.post("/validacao/correr", data={"csrf": csrf(c, "/validacao"), "modo": "tudo"}, follow_redirects=True)
        assert r.status_code == 200 and "já há uma validação" in r.get_data(as_text=True).lower()
    finally:
        validation.mark_idle()


def test_route_modo_pendentes_keeps_previously_passing_items_byte_for_byte(panel_env, monkeypatch):
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    run_and_wait(c, modo="tudo")
    before = {r["id"]: r for r in validation.load_last(db.connect(app.config["DB_PATH"]))["results"]}
    run_and_wait(c, modo="pendentes")
    after = {r["id"]: r for r in validation.load_last(db.connect(app.config["DB_PATH"]))["results"]}
    still_passing = [iid for iid, r in before.items() if r["status"] == PASSOU]
    assert still_passing
    for iid in still_passing:
        assert after[iid]["duration_s"] == before[iid]["duration_s"]   # exatamente o mesmo registo, não recalculado


def test_route_modo_tudo_reverifies_everything_again(panel_env, monkeypatch):
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    run_and_wait(c, modo="tudo")
    before = validation.load_last(db.connect(app.config["DB_PATH"]))
    run_and_wait(c, modo="tudo")
    after = validation.load_last(db.connect(app.config["DB_PATH"]))
    assert after["ts"] > before["ts"]
    assert all(r["obtained"] != "anterior" for r in after["results"])


# ---------- alternativa sem JavaScript (achado real de testes de uso) ----------
def test_running_without_javascript_still_works_via_a_real_form_post(panel_env, monkeypatch):
    """Sem o cabeçalho que o JS manda, a rota tem de continuar a arrancar a validação a sério — só responde
    de forma diferente (redirecionamento em vez de JSON), para um <form> normal funcionar sem script nenhum."""
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    r = c.post("/validacao/correr", data={"csrf": csrf(c, "/validacao"), "modo": "tudo"}, follow_redirects=True)
    assert r.status_code == 200 and "a começar" in r.get_data(as_text=True).lower()
    t0 = time.time()
    while validation.is_running():
        if time.time() - t0 > 5.0:
            raise TimeoutError("validação não terminou a tempo")
        time.sleep(0.02)
    assert validation.load_last(db.connect(app.config["DB_PATH"])) is not None   # correu mesmo, não só um efeito visual


def test_buttons_are_real_form_submits_not_only_a_script_hook(panel_env, monkeypatch):
    """Garante contra regressão: os botões têm de estar dentro de um <form method="post"> de verdade, para
    continuarem a funcionar se o script nunca correr (CSP, extensão a bloquear, JS desligado)."""
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    run_and_wait(c)
    html = c.get("/validacao").get_data(as_text=True)
    assert re.search(r'<form[^>]*action="/validacao/correr"[^>]*>.*?data-val-run="pendentes"', html, re.S)
    assert re.search(r'<form[^>]*action="/validacao/correr"[^>]*>.*?data-val-run="tudo"', html, re.S)


# ---------- a página avisa quando já há uma validação a decorrer noutro separador ----------
def test_fresh_page_load_shows_running_state_instead_of_never_ran(panel_env, monkeypatch):
    c, app = panel_env
    monkeypatch.setattr(validation, "_run_suite", fake_run_suite(all_passed_outcomes()))
    try:
        validation.begin()                                     # simula uma validação já a decorrer, noutro separador
        html = c.get("/validacao").get_data(as_text=True)
        assert "nenhuma validação" not in html.lower()
        assert "a decorrer agora" in html
    finally:
        validation.mark_idle()


def test_fresh_page_load_when_nothing_is_running_still_says_so(panel_env):
    c, app = panel_env
    html = c.get("/validacao").get_data(as_text=True)
    assert "a decorrer agora" not in html


# ---------- a página passou a viver dentro de Configuração, não na navegação principal ----------
def test_validation_link_lives_under_configuracao_not_the_main_nav(panel_env):
    c, app = panel_env
    home_html = c.get("/").get_data(as_text=True)
    assert 'href="/validacao"' not in home_html
    cfg_html = c.get("/configuracao").get_data(as_text=True)
    assert 'href="/validacao"' in cfg_html


def test_the_visual_style_file_was_not_touched():
    import subprocess
    from pathlib import Path
    out = subprocess.run(["git", "diff", "--stat", "--", "app/static/app.css"], capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parent.parent))
    assert out.stdout.strip() == ""
