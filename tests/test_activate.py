"""Botão ATIVAR: depois do PARAR TUDO cada bot parado pode ser ativado individualmente, um a um. Nunca em bloco, nunca sozinho.
O PARAR TUDO em si não muda (ver test_emergency.py)."""
import pytest

from app import botstore, db, runner
from app.engine import PAUSED, PENDING, RUNNING, STOPPED
from test_grid import flat, new_engine, run
from test_grid import candles as gcandles
from test_testnet import PAIR, T0, csrf, engine, panel, tick, world  # noqa: F401


def three_testnet_bots(conn, ex):
    bids = [botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet") for _ in range(3)]
    for b in bids:
        for k in (1, 2):
            tick(conn, ex, b, k)
    return bids


def stop_everything(c, conn, ex, k=3):
    c.post("/parar-tudo", data={"csrf": csrf(c, "/parar-tudo")})
    ex.now = T0 + k * 60_000
    runner.emergency_sweep(conn, ex, ex.now)


def activate(c, bid, url="/bots"):
    return c.post(f"/bots/{bid}/comando", data={"csrf": csrf(c, url), "cmd": "activate"}, follow_redirects=True)


def route_of(app):
    return next(r.rule for r in app.url_map.iter_rules() if r.endpoint == "bot_command").replace("<int:bot_id>", "{}")


def test_after_stop_all_every_bot_keeps_its_own_activate_button_and_there_is_no_activate_all(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bids = three_testnet_bots(conn, ex)
    stop_everything(c, conn, ex)
    assert [botstore.get(conn, b)["status"] for b in bids] == ["stopped"] * 3      # não foram apagados
    page = c.get("/bots").get_data(as_text=True)
    assert page.count(">Ativar</button>") == 3                                     # um botão por bot
    for b in bids:
        assert 'action="/bots/%d/comando"' % b in page and f"/bots/{b}/comando" in page
        assert ">Ativar</button>" in c.get(f"/bots/{b}").get_data(as_text=True)
    low = page.lower()
    assert "ativar todos" not in low and "ativar tudo" not in low


def test_nothing_restarts_by_itself_and_the_system_flag_is_not_a_bot_activation(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bids = three_testnet_bots(conn, ex)
    stop_everything(c, conn, ex)
    c.post("/reativar", data={"csrf": csrf(c, "/reativar")})                       # sai do modo seguro...
    assert db.get(conn, "emergency_stop") == "0"
    for k in (4, 5, 6):
        for b in bids:
            tick(conn, ex, b, k)
    assert [botstore.get(conn, b)["status"] for b in bids] == ["stopped"] * 3      # ...e os bots continuam parados


def test_activate_is_refused_while_the_system_is_still_in_safe_mode(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bids = three_testnet_bots(conn, ex)
    stop_everything(c, conn, ex)
    page = activate(c, bids[0]).get_data(as_text=True)
    assert "modo seguro" in page and botstore.get(conn, bids[0])["command"] is None
    tick(conn, ex, bids[0], 4)
    assert botstore.get(conn, bids[0])["status"] == "stopped"
    db.set_many(conn, {"emergency_stop": "1", "x": "1"})                           # mesmo que um pedido antigo ficasse guardado
    botstore.set_command(conn, bids[0], "activate")
    tick(conn, ex, bids[0], 5)
    assert botstore.get(conn, bids[0])["status"] == "stopped"                      # o PARAR TUDO ligado ganha sempre


def test_activating_one_bot_starts_only_that_one_with_history_kept(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bids = three_testnet_bots(conn, ex)
    before = engine(conn, bids[0])
    stop_everything(c, conn, ex)
    stopped = engine(conn, bids[0])
    fills_before = len(botstore.fills(conn, bids[0], 1000))
    c.post("/reativar", data={"csrf": csrf(c, "/reativar")})
    page = activate(c, bids[0], "/bots").get_data(as_text=True)
    assert "Pedido enviado" in page
    for k in (5, 6):
        for b in bids:
            tick(conn, ex, b, k)
    eng = engine(conn, bids[0])
    assert eng.status == RUNNING and eng.orders                                    # voltou a trabalhar
    assert [botstore.get(conn, b)["status"] for b in bids[1:]] == ["stopped", "stopped"]     # os outros não
    assert eng.s["uid"] == before.s["uid"] and eng.s["gen"] > stopped.s["gen"]    # mesma identidade, grelha nova
    for key in ("realized", "cycles", "started_ts"):
        assert eng.s[key] == stopped.s[key], key                                   # nada do passado se perdeu
    assert eng.s["fees"] >= stopped.s["fees"] > 0                                  # (as comissões só somam: a compra inicial paga a sua)
    assert len(botstore.fills(conn, bids[0], 1000)) >= fills_before
    assert eng.reason == "" and "stop_outcome" not in eng.s
    assert all(n == 1 for n in ex.placed.values())                                 # nenhum id repetido


def test_a_bot_that_is_still_stopping_or_was_reset_cannot_be_activated(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    for k in (1, 2):
        tick(conn, ex, bid, k)
    page = activate(c, bid).get_data(as_text=True)                                 # a trabalhar: não é um bot parado
    assert "ainda está a parar" in page and botstore.get(conn, bid)["command"] is None
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 3)
    eng = engine(conn, bid)
    eng.s["testnet_reset"] = True
    botstore.save_engine(conn, eng)
    page = activate(c, bid).get_data(as_text=True)
    assert "Cria um bot novo" in page and botstore.get(conn, bid)["status"] == "stopped"


def test_a_bot_that_kept_its_coin_resumes_its_own_grid(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {"never_sell_below_cost": True}, mode="testnet")
    for k in (1, 2):
        tick(conn, ex, bid, k)
    eng = engine(conn, bid)
    eng.command("stop", T0, close=50.0)                                            # abaixo do custo: fica com a moeda
    botstore.save_engine(conn, eng)
    tick(conn, ex, bid, 3)
    kept = engine(conn, bid)
    assert kept.status == STOPPED and kept.s["stop_outcome"] == "position_kept" and kept.s["base"] > 0
    activate(c, bid)
    for k in (4, 5):
        tick(conn, ex, bid, k)
    eng = engine(conn, bid)
    assert eng.status == RUNNING and eng.s["base"] == pytest.approx(kept.s["base"])     # a moeda continua a ser do bot
    assert eng.grid == kept.grid or eng.grid["slots"]                              # retomou a grelha que tinha
    assert any(o["side"] == "sell" for o in eng.orders)
    assert all(n == 1 for n in ex.placed.values())


def test_simulation_bot_can_be_activated_too_and_keeps_its_results():
    e = run(new_engine(), gcandles(flat(20)))
    e.s["realized"], e.s["cycles"], e.s["fees"] = 1.25, 3, 0.4
    e.command("stop", e.last_ts + 60_000, close=100.0)
    assert e.status == STOPPED
    realized, cycles, fees = e.s["realized"], e.s["cycles"], e.s["fees"]           # depois da liquidação do stop
    assert e.activate(e.last_ts + 120_000) is True and e.status == PENDING and e.last_ts == 0
    run(e, gcandles(flat(1), start=1_700_000_000_000))                              # a 1.ª vela monta a grelha nova
    assert e.status in (RUNNING, PAUSED) and e.grid and e.orders
    assert (e.s["realized"], e.s["cycles"]) == (realized, cycles) and e.s["fees"] >= fees    # nada do passado se perdeu
    assert e.equity(e.s["last_close"]) == pytest.approx(e.s["quote"] + e.s["reserve"] + e.s["base"] * e.s["last_close"])


def test_activate_does_nothing_on_a_bot_that_is_not_stopped_or_has_work_pending():
    e = run(new_engine(), gcandles(flat(5)))
    assert e.status == RUNNING and e.activate(1) is False                          # a trabalhar: nada a ativar
    e.command("stop", e.last_ts + 60_000, close=100.0)
    e.s["cancel_queue"] = [{"cid": "x", "slot": 0}]
    assert e.activate(2) is False and e.status == STOPPED                          # ainda há cancelamentos por confirmar
    e.s["cancel_queue"] = []
    assert e.activate(3) is True


def test_the_panel_style_is_reused_for_the_button():
    """Só classes que já existem (btn, btn pri, btn small); nenhum estilo novo."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app"
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    for cls in (".btn", ".btn.pri", ".btn.small"):
        assert cls in css
    for tpl in ("bots.html", "bot_detail.html"):
        text = (root / "templates" / tpl).read_text(encoding="utf-8")
        assert "<style" not in text.split("activate")[1][:400]


def test_the_real_runner_loop_applies_the_activate_of_a_stopped_bot(world):
    """Pelo caminho que o corredor usa (tick_all), não só por tick_bot: um bot parado sem trabalho pendente também é visitado."""
    conn, ex, bid = world
    for k in (1, 2):
        tick(conn, ex, bid, k)
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 3)
    assert botstore.get(conn, bid)["status"] == "stopped" and not botstore.has_pending(conn, botstore.get(conn, bid))
    botstore.set_command(conn, bid, "activate")
    ex.now = T0 + 4 * 60_000
    runner.tick_all(conn, ex, ex.now, trader=ex)
    ex.now = T0 + 5 * 60_000
    runner.tick_all(conn, ex, ex.now, trader=ex)
    row = botstore.get(conn, bid)
    assert row["status"] == "running" and row["command"] is None and ex.open_cids()
    runner.tick_all(conn, ex, T0 + 6 * 60_000, trader=ex)                          # e depois trabalha normalmente
    assert botstore.get(conn, bid)["status"] == "running"


def test_a_stopped_bot_without_a_pending_activate_is_still_left_alone(world):
    conn, ex, bid = world
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 1)
    assert botstore.get(conn, bid)["status"] == "stopped"
    calls = ex.calls.get("open_orders", 0)
    runner.tick_all(conn, ex, T0 + 2 * 60_000, trader=ex)
    assert ex.calls.get("open_orders", 0) == calls and botstore.get(conn, bid)["status"] == "stopped"


# ---------- pontos 1-4 do pedido: PARAR TUDO não elimina nada, preserva config e histórico ----------
def test_stop_all_stops_every_active_bot_deletes_nothing_and_keeps_config_and_history(panel):
    """1) PARAR TUDO para todos os ativos; 2) não elimina bots; 3) preserva a configuração; 4) preserva o histórico."""
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bids = three_testnet_bots(conn, ex)
    sim_bid = botstore.create(conn, PAIR, 50.0, {**ex.rules}, {"never_sell_below_cost": True}, mode="sim")
    for k in (1, 2):
        tick(conn, ex, sim_bid, k)
    before = {b: dict(botstore.get(conn, b)) for b in bids + [sim_bid]}
    counts_before = {b: (len(conn.execute("SELECT 1 FROM bot_trade_registry WHERE bot_id = ?", (b,)).fetchall()),
                         len(botstore.fills(conn, b, 1000)), len(botstore.events(conn, b, 1000))) for b in bids + [sim_bid]}
    stop_everything(c, conn, ex)
    tick(conn, ex, sim_bid, 3)                                                    # o bot em simulação também para
    all_bots = [botstore.get(conn, b) for b in bids + [sim_bid]]
    assert all(r["status"] == "stopped" for r in all_bots)                        # 1) todos parados
    assert len(botstore.all_bots(conn)) == 4                                      # 2) nenhum bot apagado
    for b in bids + [sim_bid]:
        row = botstore.get(conn, b)
        for key in ("pair", "mode", "capital_usdt", "params", "rules", "source", "twin_of"):
            assert row[key] == before[b][key], (b, key)                          # 3) configuração intacta
        after = (len(conn.execute("SELECT 1 FROM bot_trade_registry WHERE bot_id = ?", (b,)).fetchall()),
                len(botstore.fills(conn, b, 1000)), len(botstore.events(conn, b, 1000)))
        assert all(a >= before_v for a, before_v in zip(after, counts_before[b])), (b, after, counts_before[b])  # 4) histórico


# ---------- ponto 9: não existe ATIVAR TODOS (endpoint nem botão) ----------
def test_there_is_no_activate_all_endpoint_or_action_anywhere(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bids = three_testnet_bots(conn, ex)
    stop_everything(c, conn, ex)
    endpoints = {r.endpoint: r.rule for r in app.url_map.iter_rules()}
    for name, rule in endpoints.items():
        assert "activate-all" not in name and "activate_all" not in name and "ativar-todos" not in rule
    for path in ("/bots/ativar-todos", "/bots/activate-all", "/ativar-todos"):
        assert c.post(path, data={"csrf": csrf(c, "/bots")}).status_code == 404
    r = c.post("/bots/comando", data={"csrf": csrf(c, "/bots"), "cmd": "activate"})    # sem id de bot: sem rota
    assert r.status_code == 404
    for html in (c.get("/bots").get_data(as_text=True), c.get(f"/bots/{bids[0]}").get_data(as_text=True)):
        low = html.lower()
        assert "ativar todos" not in low and "activate all" not in low and "ativar tudo" not in low


# ---------- ponto 8/11: reativar duas vezes nunca duplica; e respeita STOPPING genuíno ----------
def test_activating_twice_in_a_row_never_duplicates_orders_or_ids(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    for k in (1, 2):
        tick(conn, ex, bid, k)
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 3)
    assert botstore.get(conn, bid)["status"] == "stopped"
    activate(c, bid)
    for k in (4, 5):
        tick(conn, ex, bid, k)
    assert botstore.get(conn, bid)["status"] == "running"
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 6)
    assert botstore.get(conn, bid)["status"] == "stopped"
    activate(c, bid)                                                              # ativa outra vez o MESMO bot
    for k in (7, 8):
        tick(conn, ex, bid, k)
    eng = engine(conn, bid)
    assert eng.status == "running"
    assert all(n == 1 for n in ex.placed.values())                                # nenhum id enviado duas vezes em nenhuma volta
    assert len({o["cid"] for o in eng.orders if o.get("cid")}) == len([o for o in eng.orders if o.get("cid")])


def test_activate_is_refused_while_genuinely_stopping_and_works_once_it_confirms(panel):
    from app.trader import TraderError
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    for k in (1, 2):
        tick(conn, ex, bid, k)
    ex.persist_fail["cancel"] = TraderError("Sem ligação à Testnet.")             # a paragem não consegue terminar
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 3)
    assert botstore.get(conn, bid)["status"] == "stopping"                       # ainda STOPPING, não STOPPED
    page = activate(c, bid).get_data(as_text=True)
    assert "ainda está a parar" in page and botstore.get(conn, bid)["command"] is None
    del ex.persist_fail["cancel"]
    tick(conn, ex, bid, 4)
    assert botstore.get(conn, bid)["status"] == "stopped"                        # agora sim, confirmado
    activate(c, bid)
    tick(conn, ex, bid, 5)
    assert botstore.get(conn, bid)["status"] == "running"


# ---------- ponto 12: reiniciar o corredor depois de PARAR TUDO não reativa nada ----------
def test_restarting_the_runner_after_stop_all_never_reactivates_bots_automatically(panel):
    from test_recovery import db_path
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bids = three_testnet_bots(conn, ex)
    stop_everything(c, conn, ex)
    assert all(botstore.get(conn, b)["status"] == "stopped" for b in bids)
    path = db_path(conn)
    conn.close()
    conn = db.connect(path)                                                       # "reiniciar o corredor": nova ligação
    for k in (4, 5, 6):
        ex.now = T0 + k * 60_000
        runner.tick_all(conn, ex, ex.now, trader=ex)
    assert all(botstore.get(conn, b)["status"] == "stopped" for b in bids)        # continuam parados, sem intervenção
    assert all(botstore.get(conn, b)["command"] is None for b in bids)            # e sem nenhum pedido criado do nada
    c.post("/reativar", data={"csrf": csrf(c, "/reativar")})                       # sai do modo seguro (não ativa nada por si só)
    for k in (6, 7):
        ex.now = T0 + k * 60_000
        runner.tick_all(conn, ex, ex.now, trader=ex)
    assert all(botstore.get(conn, b)["status"] == "stopped" for b in bids)        # ainda assim continuam parados
    activate(c, bids[0], "/bots")                                                 # só agora, um pedido explícito
    for k in (8, 9):
        ex.now = T0 + k * 60_000
        runner.tick_all(conn, ex, ex.now, trader=ex)
    assert botstore.get(conn, bids[0])["status"] == "running"
    assert [botstore.get(conn, b)["status"] for b in bids[1:]] == ["stopped", "stopped"]
