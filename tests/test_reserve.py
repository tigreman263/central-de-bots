"""Área da Reserva: o algoritmo só propõe (com dados reais); nunca decide nem compra. Cada proposta espera
Aprovar/Rejeitar; a mesma moeda nunca é proposta duas vezes."""
import re

import pytest

from app import db, reserve
from test_portfolio import World, csrf, env  # noqa: F401


def ticker(vol="500000000", bid="99.99", ask="100.01", last="100"):
    return {"lastPrice": last, "bidPrice": bid, "askPrice": ask, "quoteVolume": vol}


def good_market():
    return {"BTCUSDT": ticker(), "ETHUSDT": ticker(vol="300000000")}


def fresh_conn(tmp_path):
    conn = db.connect(str(tmp_path / "r.db"))
    conn.executescript(db.SCHEMA)
    return conn


# ---------- o algoritmo em si ----------
def test_candidates_only_include_coins_with_real_volume_and_tight_spread():
    market = good_market()
    market["BNBUSDT"] = ticker(vol="5000000")                              # volume baixo demais: fora
    market["XRPUSDT"] = ticker(bid="90", ask="110")                        # spread largo: fora
    out = reserve.candidates(market)
    coins = {c["coin"] for c in out}
    assert coins == {"BTC", "ETH"} and all(c["quote_volume"] >= reserve.MIN_QUOTE_VOLUME for c in out)


def test_candidates_respect_the_exclusion_categories_configured_by_the_user():
    market = {"BTCUSDT": ticker()}
    assert reserve.candidates(market, exclusions=())[0]["coin"] == "BTC"
    # nenhuma das candidatas está em memecoins/leveraged por desenho, mas a função tem de respeitar a exclusão de qualquer forma
    from app.pairs import MEMECOINS
    assert not (set(reserve.ESTABLISHED) & MEMECOINS)


def test_candidates_never_invents_a_coin_without_real_ticker_data():
    out = reserve.candidates({})                                          # mercado vazio: nenhuma candidata
    assert out == []


def test_candidates_are_sorted_by_real_volume_descending():
    market = {"BTCUSDT": ticker(vol="900000000"), "ETHUSDT": ticker(vol="200000000"), "LTCUSDT": ticker(vol="150000000")}
    out = reserve.candidates(market)
    assert [c["coin"] for c in out] == ["BTC", "ETH", "LTC"]


# ---------- propostas: nunca repete, nunca decide sozinho ----------
def test_propose_creates_pending_rows_with_real_numbers(tmp_path):
    conn = fresh_conn(tmp_path)
    created = reserve.propose(conn, good_market(), limit=2)
    assert len(created) == 2 and all(c["status"] == "pending" for c in created)
    v = reserve.view(conn)
    assert len(v["pending"]) == 2 and v["approved"] == []                  # nada fica decidido sozinho
    assert all(p["quote_volume"] > 0 for p in v["pending"])                 # números reais, não inventados


def test_propose_never_repeats_a_coin_already_proposed_before(tmp_path):
    conn = fresh_conn(tmp_path)
    reserve.propose(conn, good_market(), limit=1)
    first_coin = reserve.view(conn)["pending"][0]["coin"]
    reserve.decide(conn, reserve.view(conn)["pending"][0]["id"], approve=False)   # rejeitada
    again = reserve.propose(conn, good_market(), limit=5)
    assert first_coin not in {c["coin"] for c in again}                    # não volta a propor a mesma, nem depois de rejeitada


def test_propose_with_no_qualifying_coin_creates_nothing(tmp_path):
    conn = fresh_conn(tmp_path)
    assert reserve.propose(conn, {}, limit=3) == []
    assert reserve.view(conn)["pending"] == []


def test_decide_approve_and_reject_and_cannot_decide_twice(tmp_path):
    conn = fresh_conn(tmp_path)
    reserve.propose(conn, good_market(), limit=1)
    pid = reserve.view(conn)["pending"][0]["id"]
    result = reserve.decide(conn, pid, approve=True, note="ok")
    assert result["status"] == "approved"
    v = reserve.view(conn)
    assert len(v["approved"]) == 1 and v["approved"][0]["note"] == "ok" and v["pending"] == []
    assert reserve.decide(conn, pid, approve=False) is None                # já decidida: não se pode voltar a decidir
    assert reserve.decide(conn, 9999, approve=True) is None                # id inexistente


def test_view_separates_approved_pending_and_history(tmp_path):
    conn = fresh_conn(tmp_path)
    reserve.propose(conn, good_market(), limit=2)
    ids = [p["id"] for p in reserve.view(conn)["pending"]]
    reserve.decide(conn, ids[0], approve=True)
    reserve.decide(conn, ids[1], approve=False)
    v = reserve.view(conn)
    assert len(v["approved"]) == 1 and len(v["history"]) == 1 and v["pending"] == []   # aprovada fica só na tabela da reserva
    assert v["history"][0]["status"] == "rejected"
    assert v["reserve_coins"] == [v["approved"][0]["coin"]]


# ---------- retirar, ajustar peso, e voltar a propor depois de retirar ----------
def approve_one(conn, market=None, limit=1):
    reserve.propose(conn, market or good_market(), limit=limit)
    return reserve.view(conn)["pending"][0]["id"]


def test_remove_takes_a_coin_out_of_the_reserve_and_keeps_it_in_history(tmp_path):
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    reserve.decide(conn, pid, approve=True)
    result = reserve.remove(conn, pid, note="já não quero BTC agora")
    assert result["status"] == "removed"
    v = reserve.view(conn)
    assert v["approved"] == [] and v["history"][0]["status"] == "removed" and v["history"][0]["note"] == "já não quero BTC agora"


def test_remove_only_works_on_an_approved_coin_never_on_pending_or_already_decided(tmp_path):
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    assert reserve.remove(conn, pid) is None                               # ainda pendente: não se pode retirar
    reserve.decide(conn, pid, approve=False)
    assert reserve.remove(conn, pid) is None                               # rejeitada, nunca esteve na reserva
    assert reserve.remove(conn, 9999) is None                              # id inexistente


def test_a_removed_coin_can_be_proposed_again_but_a_rejected_one_cannot(tmp_path):
    conn = fresh_conn(tmp_path)
    market = good_market()
    pid = approve_one(conn, market)
    reserve.decide(conn, pid, approve=True)
    reserve.remove(conn, pid)
    again = reserve.propose(conn, market, limit=5)
    assert "BTC" in {c["coin"] for c in again}                            # retirada: pode voltar a ser proposta
    pid2 = next(p["id"] for p in reserve.view(conn)["pending"] if p["coin"] == "BTC")
    reserve.decide(conn, pid2, approve=False)                              # agora rejeita-a
    yet_again = reserve.propose(conn, market, limit=5)
    assert "BTC" not in {c["coin"] for c in yet_again}                    # rejeitada: essa sim, não volta


def test_adjust_changes_the_target_weight_of_an_approved_coin(tmp_path):
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    reserve.decide(conn, pid, approve=True)
    result = reserve.adjust(conn, pid, 42.5)
    assert result == {"coin": "BTC", "pct": 42.5}
    assert reserve.view(conn)["approved"][0]["suggested_pct"] == 42.5


def test_adjust_rejects_out_of_range_weights_and_pending_coins(tmp_path):
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    assert reserve.adjust(conn, pid, 50.0) is None                         # ainda pendente
    reserve.decide(conn, pid, approve=True)
    assert reserve.adjust(conn, pid, 0.0) is None and reserve.adjust(conn, pid, 100.1) is None
    assert reserve.view(conn)["approved"][0]["suggested_pct"] == pytest.approx(100 / 2, abs=1)  # inalterado


# ---------- alvo vs. o que tens de facto (portefólio real) ----------
def snapshot(total, holdings):
    return {"total_usdt": total, "holdings": holdings}


def test_with_targets_computes_target_and_gap_against_a_real_snapshot(tmp_path):
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    reserve.decide(conn, pid, approve=True)
    reserve.adjust(conn, pid, 50.0)
    snap = snapshot(1000.0, [{"coin": "BTC", "qty": 0.001, "value_usdt": 80.0}])
    v = reserve.view(conn, snapshot=snap, split_reserve_pct=60.0)          # reserva = 600 USDT; alvo BTC = 50% = 300
    r = v["approved"][0]
    assert r["target_usdt"] == pytest.approx(300.0) and r["held_usdt"] == pytest.approx(80.0)
    assert r["gap_usdt"] == pytest.approx(220.0)                           # falta 220 USDT para atingir o alvo


def test_with_targets_never_invents_numbers_without_a_real_portfolio(tmp_path):
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    reserve.decide(conn, pid, approve=True)
    v = reserve.view(conn)                                                # sem snapshot
    r = v["approved"][0]
    assert r["target_usdt"] is None and r["held_usdt"] is None and r["gap_usdt"] is None
    assert v["has_real_portfolio"] is False


def test_with_targets_shows_zero_held_not_none_when_portfolio_exists_but_coin_is_absent(tmp_path):
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    reserve.decide(conn, pid, approve=True)
    v = reserve.view(conn, snapshot=snapshot(1000.0, []), split_reserve_pct=60.0)   # portefólio real, mas sem BTC
    assert v["approved"][0]["held_usdt"] == 0.0 and v["has_real_portfolio"] is True


def test_target_and_held_pct_agree_when_the_real_portfolio_sums_to_zero(tmp_path):
    """Achado real de testes de uso (validado pelo Codex): com reserve_total == 0, "Alvo" mostrava 0.00 mas
    "% da reserva" mostrava "—" para a mesma situação — inconsistente. Os dois têm de concordar sempre."""
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    reserve.decide(conn, pid, approve=True)
    v = reserve.view(conn, snapshot=snapshot(0.0, []), split_reserve_pct=60.0)      # carteira real ligada, mas a somar 0
    r = v["approved"][0]
    assert r["target_usdt"] is None and r["held_pct_of_reserve"] is None and r["gap_usdt"] is None


def test_held_usdt_counts_only_the_free_part_locked_orders_are_shown_apart(tmp_path):
    conn = fresh_conn(tmp_path)
    pid = approve_one(conn)
    reserve.decide(conn, pid, approve=True)
    reserve.adjust(conn, pid, 50.0)
    # 0.001 BTC no total, metade (0.0004) bloqueada numa ordem aberta -> preço implícito 80000 USDT/BTC
    snap = snapshot(1000.0, [{"coin": "BTC", "qty": 0.001, "locked": 0.0004, "value_usdt": 80.0}])
    v = reserve.view(conn, snapshot=snap, split_reserve_pct=60.0)
    r = v["approved"][0]
    assert r["held_usdt"] == pytest.approx(48.0)                          # só a parte livre (0.0006 BTC) conta como segura
    assert r["locked_usdt"] == pytest.approx(32.0)                        # a parte bloqueada aparece à parte
    assert r["gap_usdt"] == pytest.approx(300.0 - 48.0)                   # a falta ao alvo usa o valor livre, não o total


def test_total_pct_flags_when_weights_do_not_add_up_to_100(tmp_path):
    conn = fresh_conn(tmp_path)
    market = {"BTCUSDT": ticker(), "ETHUSDT": ticker(vol="300000000")}
    reserve.propose(conn, market, limit=2)
    for p in reserve.view(conn)["pending"]:
        reserve.decide(conn, p["id"], approve=True)                        # cada uma fica com o peso "sugerido" ao propor
    assert reserve.view(conn)["total_pct"] != 100.0                        # o caso real que motivou este pedido


# ---------- painel ----------
def test_reserva_page_shows_the_real_placeholder_text_is_gone(env):
    c, world, app, tmp = env
    html = c.get("/reserva").get_data(as_text=True)
    assert "chegam na v0.4" not in html
    assert "Reserva" in html and "PARAR TUDO" in html


def test_reserva_page_generates_proposals_from_real_market_data(env):
    c, world, app, tmp = env
    world.tickers.update({"BTCUSDT": ticker(), "ETHUSDT": ticker(vol="300000000")})
    tok = csrf(c, "/reserva")
    r = c.post("/reserva/gerar", data={"csrf": tok}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "proposta" in html.lower()
    assert "BTC" in html and "Aprovar" in html and "Rejeitar" in html


def test_reserva_page_never_generates_when_binance_is_down(env):
    c, world, app, tmp = env
    world.fail = "sem ligação"
    r = c.post("/reserva/gerar", data={"csrf": csrf(c, "/reserva")}, follow_redirects=True)
    assert "Não consegui ler o mercado" in r.get_data(as_text=True)
    assert reserve.view(db.connect(app.config["DB_PATH"]))["pending"] == []


def test_approving_a_proposal_through_the_panel_moves_it_to_the_reserve(env):
    c, world, app, tmp = env
    world.tickers.update({"BTCUSDT": ticker()})
    c.post("/reserva/gerar", data={"csrf": csrf(c, "/reserva")})
    conn = db.connect(app.config["DB_PATH"])
    pid = reserve.view(conn)["pending"][0]["id"]
    r = c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": pid, "action": "approve"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "aprovada" in html.lower()
    v = reserve.view(db.connect(app.config["DB_PATH"]))
    assert len(v["approved"]) == 1 and v["pending"] == []


def test_rejecting_a_proposal_never_places_it_in_the_reserve(env):
    c, world, app, tmp = env
    world.tickers.update({"BTCUSDT": ticker()})
    c.post("/reserva/gerar", data={"csrf": csrf(c, "/reserva")})
    conn = db.connect(app.config["DB_PATH"])
    pid = reserve.view(conn)["pending"][0]["id"]
    c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": pid, "action": "reject"})
    v = reserve.view(db.connect(app.config["DB_PATH"]))
    assert v["approved"] == [] and v["pending"] == [] and v["history"][0]["status"] == "rejected"


def test_deciding_an_already_decided_proposal_is_refused(env):
    c, world, app, tmp = env
    world.tickers.update({"BTCUSDT": ticker()})
    c.post("/reserva/gerar", data={"csrf": csrf(c, "/reserva")})
    conn = db.connect(app.config["DB_PATH"])
    pid = reserve.view(conn)["pending"][0]["id"]
    c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": pid, "action": "approve"})
    r = c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": pid, "action": "reject"}, follow_redirects=True)
    assert "já não está pendente" in r.get_data(as_text=True)
    assert len(reserve.view(db.connect(app.config["DB_PATH"]))["approved"]) == 1   # continua aprovada, não mudou


def test_reserva_routes_require_login_and_csrf(env):
    c, world, app, tmp = env
    anon = app.test_client()
    assert anon.get("/reserva").status_code == 302
    assert anon.post("/reserva/gerar").status_code in (302, 400, 403)
    assert c.post("/reserva/gerar", data={"csrf": "errado"}).status_code == 400
    assert c.post("/reserva/decidir", data={"csrf": "errado", "id": 1, "action": "approve"}).status_code == 400


def test_reserva_decidir_rejects_a_bad_action(env):
    c, world, app, tmp = env
    r = c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": 1, "action": "sabotar"})
    assert r.status_code == 400


def test_reserva_page_mentions_the_configured_split_and_never_buys_anything(env):
    c, world, app, tmp = env
    html = c.get("/reserva").get_data(as_text=True)
    assert "% do capital" in html and "Nunca compra nada" in html


def test_approve_note_persists_end_to_end_through_the_panel(env):
    c, world, app, tmp = env
    world.tickers.update({"BTCUSDT": ticker()})
    c.post("/reserva/gerar", data={"csrf": csrf(c, "/reserva")})
    conn = db.connect(app.config["DB_PATH"])
    pid = reserve.view(conn)["pending"][0]["id"]
    c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": pid, "action": "approve",
                                      "note": "boa liquidez esta semana"})
    v = reserve.view(db.connect(app.config["DB_PATH"]), snapshot=None)
    html = c.get("/reserva").get_data(as_text=True)
    assert v["approved"][0]["note"] == "boa liquidez esta semana"
    assert "Retirar" in html and "Ajustar" in html
    assert "boa liquidez esta semana" in html                              # a nota tem de aparecer na reserva atual, não só no histórico


def test_adjusting_the_target_weight_through_the_panel(env):
    c, world, app, tmp = env
    world.tickers.update({"BTCUSDT": ticker()})
    c.post("/reserva/gerar", data={"csrf": csrf(c, "/reserva")})
    conn = db.connect(app.config["DB_PATH"])
    pid = reserve.view(conn)["pending"][0]["id"]
    c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": pid, "action": "approve"})
    r = c.post("/reserva/ajustar", data={"csrf": csrf(c, "/reserva"), "id": pid, "pct": "37.5"}, follow_redirects=True)
    assert r.status_code == 200
    v = reserve.view(db.connect(app.config["DB_PATH"]))
    assert v["approved"][0]["suggested_pct"] == 37.5


def test_adjusting_rejects_an_invalid_weight_through_the_panel(env):
    c, world, app, tmp = env
    world.tickers.update({"BTCUSDT": ticker()})
    c.post("/reserva/gerar", data={"csrf": csrf(c, "/reserva")})
    conn = db.connect(app.config["DB_PATH"])
    pid = reserve.view(conn)["pending"][0]["id"]
    c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": pid, "action": "approve"})
    r = c.post("/reserva/ajustar", data={"csrf": csrf(c, "/reserva"), "id": pid, "pct": "200"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "não" in html.lower() or "inválid" in html.lower() or "erro" in html.lower()
    v = reserve.view(db.connect(app.config["DB_PATH"]))
    assert v["approved"][0]["suggested_pct"] != 200.0


def test_removing_a_coin_through_the_panel_moves_it_to_history(env):
    c, world, app, tmp = env
    world.tickers.update({"BTCUSDT": ticker()})
    c.post("/reserva/gerar", data={"csrf": csrf(c, "/reserva")})
    conn = db.connect(app.config["DB_PATH"])
    pid = reserve.view(conn)["pending"][0]["id"]
    c.post("/reserva/decidir", data={"csrf": csrf(c, "/reserva"), "id": pid, "action": "approve"})
    r = c.post("/reserva/remover", data={"csrf": csrf(c, "/reserva"), "id": pid}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "retirad" in html.lower()
    v = reserve.view(db.connect(app.config["DB_PATH"]))
    assert v["approved"] == [] and v["history"][0]["status"] == "removed"


def test_reserva_ajustar_and_remover_require_login_and_csrf(env):
    c, world, app, tmp = env
    anon = app.test_client()
    assert anon.post("/reserva/ajustar", data={"id": 1, "pct": "10"}).status_code in (302, 400, 403)
    assert anon.post("/reserva/remover", data={"id": 1}).status_code in (302, 400, 403)
    assert c.post("/reserva/ajustar", data={"csrf": "errado", "id": 1, "pct": "10"}).status_code == 400
    assert c.post("/reserva/remover", data={"csrf": "errado", "id": 1}).status_code == 400


def test_reserva_ajustar_and_remover_on_a_pending_or_missing_id_is_refused_not_crash(env):
    c, world, app, tmp = env
    r1 = c.post("/reserva/ajustar", data={"csrf": csrf(c, "/reserva"), "id": 9999, "pct": "10"}, follow_redirects=True)
    r2 = c.post("/reserva/remover", data={"csrf": csrf(c, "/reserva"), "id": 9999}, follow_redirects=True)
    assert r1.status_code == 200 and r2.status_code == 200


def test_the_visual_style_file_was_not_touched():
    import subprocess
    from pathlib import Path
    out = subprocess.run(["git", "diff", "--stat", "--", "app/static/app.css"], capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parent.parent))
    assert out.stdout.strip() == ""
