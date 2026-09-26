"""Configuração por categorias, alertas por Telegram e WhatsApp (com explicações e teste) e apagar bots parados."""
import io
import json
import urllib.error

import pytest

from app import botstore, db, keystore, notify
from app.engine import Engine
from app.trader import TraderError
from test_testnet import PAIR, T0, csrf, engine, panel, tick, world  # noqa: F401  (fixtures)

CATS = ["capital", "risco", "portefolio", "sugestoes", "alertas", "ligacoes", "sistema"]


# ---------- configuração por categorias ----------
def test_configuration_is_split_into_categories_with_a_working_default(panel):
    c, ex, app, tmp = panel
    html = c.get("/configuracao").get_data(as_text=True)
    for code in CATS:
        assert f'data-cat-link="{code}"' in html and f'data-cat="{code}"' in html
    assert 'aria-label="Categorias da configuração"' in html
    assert "Capital e saldo" in html and "Notificações" in html and "Sistema" in html
    # só a categoria pedida fica visível no servidor (sem JavaScript também funciona)
    alertas = c.get("/configuracao?cat=alertas").get_data(as_text=True)
    assert '<section data-cat="alertas">' in alertas
    assert '<section data-cat="capital" hidden>' in alertas and 'id="cfgform" hidden>' in alertas
    assert c.get("/configuracao?cat=lixo").status_code == 200                       # categoria inválida: cai para a primeira


def test_saving_a_settings_category_keeps_you_on_it_and_still_asks_for_confirmation(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    cfg = db.get_all(conn)
    form = {"csrf": csrf(c, "/configuracao?cat=risco"), "cat": "risco", "step": "preview",
            **{k: cfg[k] for k in ("capital_eur", "split_reserve", "split_trading", "split_cash", "profit_to_reserve",
                                   "pause_drawdown")}, "max_loss_trade": "3"}
    for k, v in cfg.items():
        if k.startswith("risk_"):
            form[k] = v
    r = c.post("/configuracao", data=form)
    assert "Confirmar alterações" in r.get_data(as_text=True) and 'name="cat" value="risco"' in r.get_data(as_text=True)
    r = c.post("/configuracao", data={**form, "step": "save"})
    assert r.status_code == 302 and r.headers["Location"].endswith("cat=risco")
    assert db.get(conn, "max_loss_trade") == "3"


def test_connections_and_system_categories_show_the_real_state(panel):
    c, ex, app, tmp = panel
    html = c.get("/configuracao?cat=ligacoes").get_data(as_text=True)
    assert "Binance, chave real (só leitura)" in html and "Telegram" in html and "WhatsApp" in html
    assert "sem chave" in html and "por configurar" in html
    system = c.get("/configuracao?cat=sistema").get_data(as_text=True)
    assert "Corredor dos bots" in system and "BOTS_DATA_DIR" in system and "MB" in system


# ---------- alertas: explicações e preferências ----------
def test_alerts_area_explains_severities_channels_and_privacy(panel):
    c, ex, app, tmp = panel
    html = c.get("/configuracao?cat=alertas").get_data(as_text=True)
    for text in ("Como funcionam os alertas", "Bot parado", "Reset da Testnet", "não recebem comandos",
                 "@BotFather", "CallMeBot", "Cloud API", "Phone number ID", "nunca chaves nem valores da carteira",
                 "janela de 24 h"):
        assert text in html, text


def test_alert_preferences_are_saved_and_default_to_telegram_attention_whatsapp_high(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    assert notify.prefs(conn) == {"alert_telegram_on": "1", "alert_telegram_min": "atenção",
                                  "alert_whatsapp_on": "1", "alert_whatsapp_min": "alto"}
    c.post("/configuracao/alertas", data={"csrf": csrf(c, "/configuracao?cat=alertas"), "action": "save_prefs",
                                          "alert_telegram_min": "alto", "alert_whatsapp_min": "lixo"})
    p = notify.prefs(conn)
    assert p["alert_telegram_on"] == "0" and p["alert_whatsapp_on"] == "0"          # sem a caixa marcada: desligado
    assert p["alert_telegram_min"] == "alto" and p["alert_whatsapp_min"] == "alto"   # valor inválido: volta ao defeito
    assert c.post("/configuracao/alertas", data={"csrf": csrf(c, "/configuracao?cat=alertas"),
                                                 "action": "hack"}).status_code == 400


# ---------- WhatsApp: validação e guardar ----------
def test_whatsapp_form_is_validated_in_plain_portuguese():
    cfg, errors = notify.validate_whatsapp({"provider": "callmebot", "phone": "912 345 678", "apikey": ""})
    assert cfg is None and any("apikey" in e for e in errors)
    cfg, errors = notify.validate_whatsapp({"provider": "callmebot", "phone": "abc", "apikey": "k"})
    assert cfg is None and "formato internacional" in errors[0]
    cfg, errors = notify.validate_whatsapp({"provider": "callmebot", "phone": "+351 912 345 678", "apikey": " 123456 "})
    assert errors == [] and cfg == {"provider": "callmebot", "phone": "351912345678", "apikey": "123456"}
    cfg, errors = notify.validate_whatsapp({"provider": "cloud", "phone": "00351912345678", "phone_number_id": "1234",
                                            "token": "T", "template": "alerta_malha"})
    assert errors == [] and cfg["phone"] == "351912345678" and cfg["template"] == "alerta_malha"
    assert notify.validate_whatsapp({"provider": "cloud", "phone": "351912345678", "phone_number_id": "1", "token": "T",
                                     "template": "Nome Errado"})[0] is None
    assert notify.validate_whatsapp({"provider": "outro", "phone": "351912345678"})[0] is None


def test_whatsapp_is_tested_before_saving_and_secrets_are_never_shown(panel, isolated_notifications):
    c, ex, app, tmp = panel
    data = {"csrf": csrf(c, "/configuracao?cat=alertas"), "action": "save_whatsapp", "provider": "callmebot",
            "phone": "+351 912 345 678", "apikey": "SEGREDO-CALLMEBOT-1234"}
    r = c.post("/configuracao/alertas", data=data, follow_redirects=True)
    saved = keystore.load_json(tmp / "wa.json")
    assert saved == {"provider": "callmebot", "phone": "351912345678", "apikey": "SEGREDO-CALLMEBOT-1234"}
    html = r.get_data(as_text=True)
    assert any("[whatsapp]" in m and "mensagem de teste" in m for m in isolated_notifications)
    assert "SEGREDO-CALLMEBOT-1234" not in html and "+…678" in html
    # o teste falha: não guarda
    (tmp / "wa.json").unlink()
    old = notify.WA_SENDER
    notify.WA_SENDER = lambda cfg, text: (False, "o CallMeBot recusou (confirma o número e a chave)")
    try:
        r = c.post("/configuracao/alertas", data=data, follow_redirects=True)
    finally:
        notify.WA_SENDER = old
    assert not (tmp / "wa.json").exists() and "Não consegui enviar" in r.get_data(as_text=True)
    # guardar sem testar (Cloud API fora da janela de 24 h)
    cloud = {"csrf": csrf(c, "/configuracao?cat=alertas"), "action": "save_whatsapp_notest", "provider": "cloud",
             "phone": "351912345678", "phone_number_id": "555", "token": "TOKEN-META", "template": ""}
    c.post("/configuracao/alertas", data=cloud)
    assert keystore.load_json(tmp / "wa.json")["provider"] == "cloud"
    c.post("/configuracao/alertas", data={"csrf": csrf(c, "/configuracao?cat=alertas"), "action": "delete_whatsapp"})
    assert not (tmp / "wa.json").exists()


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self


def test_callmebot_and_cloud_api_requests_and_errors():
    seen = []

    def ok_callmebot(request, timeout=10):
        seen.append(request.full_url)
        return _Resp(b"Message queued. You will receive it in a few seconds.")
    cfg = {"provider": "callmebot", "phone": "351912345678", "apikey": "K3Y"}
    assert notify.whatsapp_send(cfg, "olá & adeus", opener=ok_callmebot) == (True, "")
    assert "phone=%2B351912345678" in seen[0] and "apikey=K3Y" in seen[0] and "ol%C3%A1+%26+adeus" in seen[0]
    bad = notify.whatsapp_send(cfg, "x", opener=lambda r, timeout=10: _Resp(b"ERROR: APIKey is invalid"))
    assert bad[0] is False and "K3Y" not in bad[1]

    calls = []

    def ok_cloud(request, timeout=10):
        calls.append((request.full_url, json.loads(request.data), dict(request.headers)))
        return _Resp(json.dumps({"messages": [{"id": "wamid"}]}).encode())
    cloud = {"provider": "cloud", "phone": "351912345678", "phone_number_id": "555", "token": "TOK", "template": ""}
    assert notify.whatsapp_send(cloud, "alerta", opener=ok_cloud) == (True, "")
    url, body, headers = calls[0]
    assert url == "https://graph.facebook.com/v21.0/555/messages" and body["type"] == "text"
    assert body["text"]["body"] == "alerta" and headers["Authorization"] == "Bearer TOK"
    assert notify.whatsapp_send({**cloud, "template": "alerta_malha"}, "alerta", opener=ok_cloud) == (True, "")
    tpl = calls[1][1]
    assert tpl["type"] == "template" and tpl["template"]["name"] == "alerta_malha"
    assert tpl["template"]["components"][0]["parameters"][0]["text"] == "alerta"

    def denied(request, timeout=10):
        body = io.BytesIO(json.dumps({"error": {"code": 190, "message": "Invalid OAuth access token."}}).encode())
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, body)
    err = notify.whatsapp_send(cloud, "x", opener=denied)
    assert err[0] is False and "190" in err[1] and "TOK" not in err[1]
    down = notify.whatsapp_send(cloud, "x", opener=lambda r, timeout=10: (_ for _ in ()).throw(OSError("rede")))
    assert down == (False, "sem ligação ao serviço de WhatsApp")


# ---------- envio por canal respeita ligado/desligado e gravidade ----------
def _channels(tmp_path):
    keystore.save(tmp_path / "tg.json", "1:tok", "42")
    keystore.save_json(tmp_path / "wa.json", {"provider": "callmebot", "phone": "351912345678", "apikey": "k"})
    notify.TELEGRAM_FILE, notify.WHATSAPP_FILE = tmp_path / "tg.json", tmp_path / "wa.json"


def test_broadcast_sends_by_channel_according_to_severity_and_switches(tmp_path, isolated_notifications):
    conn = db.connect(str(tmp_path / "b.db"))
    conn.executescript(db.SCHEMA)
    _channels(tmp_path)
    sent = isolated_notifications
    assert [r[0] for r in notify.broadcast(conn, "a", "atenção")] == ["telegram"]           # WhatsApp só desde "alto"
    assert [r[0] for r in notify.broadcast(conn, "b", "alto")] == ["telegram", "whatsapp"]
    db.set_many(conn, {"alert_telegram_on": "0", "alert_whatsapp_min": "atenção"})
    assert [r[0] for r in notify.broadcast(conn, "c", "atenção")] == ["whatsapp"]
    db.set_many(conn, {"alert_whatsapp_on": "0"})
    assert notify.broadcast(conn, "d", "alto") == []
    assert sent == ["a", "b", "[whatsapp] b", "[whatsapp] c"]


def test_a_failing_channel_never_stops_the_others_or_the_bot(tmp_path):
    conn = db.connect(str(tmp_path / "f.db"))
    conn.executescript(db.SCHEMA)
    _channels(tmp_path)
    old_tg, old_wa = notify.SENDER, notify.WA_SENDER
    notify.SENDER = lambda t, c, m: (False, "sem ligação ao Telegram")
    got = []
    notify.WA_SENDER = lambda cfg, m: got.append(m) or (True, "")
    try:
        results = notify.broadcast(conn, "x", "alto")
    finally:
        notify.SENDER, notify.WA_SENDER = old_tg, old_wa
    assert results == [("telegram", False, "sem ligação ao Telegram"), ("whatsapp", True, "")] and got == ["x"]


def test_bot_alerts_reach_the_panel_and_whatsapp_when_high(world, tmp_path, isolated_notifications):
    conn, ex, bid = world
    _channels(tmp_path)
    eng = Engine.new(PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    eng.id = bid
    notify.bot_events(conn, eng, [{"ts": 1, "kind": "pause", "detail": "queda"},
                                  {"ts": 2, "kind": "stop", "detail": "stop-loss"}], 2)
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE key LIKE 'bot:%'").fetchone()[0] == 2
    assert [m for m in isolated_notifications if m.startswith("[whatsapp]")] == [
        "[whatsapp] Malha · Bot 1 XYZUSDT (Testnet): stop-loss"]
    assert len([m for m in isolated_notifications if not m.startswith("[whatsapp]")]) == 2       # Telegram: atenção e alto


def test_the_watchdog_uses_every_enabled_channel(world, tmp_path, isolated_notifications):
    from datetime import datetime, timezone
    from app import watchdog
    conn, ex, bid = world
    _channels(tmp_path)
    now = datetime.now(timezone.utc).timestamp()
    db.set_many(conn, {"runner_heartbeat": datetime.fromtimestamp(now - 900, timezone.utc).isoformat(timespec="seconds")})
    assert watchdog.check(conn, tmp_path / "s.json", now) == "parado"
    assert any("parou de dar sinal" in m and m.startswith("[whatsapp]") for m in isolated_notifications)
    assert any("parou de dar sinal" in m and not m.startswith("[whatsapp]") for m in isolated_notifications)


# ---------- apagar bots parados ----------
def test_only_stopped_bots_without_pending_work_can_be_deleted(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    assert botstore.delete(conn, bid) is False                                      # a trabalhar: não se apaga
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 2)
    row = botstore.get(conn, bid)
    assert row["status"] == "stopped" and botstore.can_delete(conn, row)
    assert botstore.delete(conn, bid) is True
    assert botstore.get(conn, bid) is None
    for table in ("bot_orders", "bot_fills", "bot_events", "bot_equity", "bot_candles"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE bot_id = ?", (bid,)).fetchone()[0] == 0, table


def test_a_stopped_bot_with_orders_still_to_cancel_is_not_deleted(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    ex.fail["cancel"] = TraderError("Sem ligação à Testnet.")
    botstore.set_command(conn, bid, "stop")
    tick(conn, ex, bid, 2)
    row = botstore.get(conn, bid)
    assert row["status"] == "stopped" and botstore.has_pending(conn, row)
    assert botstore.delete(conn, bid) is False                                      # ainda há ordens abertas na exchange


def test_deleting_a_bot_keeps_its_twin_and_removes_its_alerts(world):
    conn, ex, bid = world
    twin = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="sim", source="testnet", twin_of=bid)
    conn.execute("UPDATE bots SET status = 'stopped' WHERE id = ?", (bid,))
    conn.commit()
    notify.alerts.init(conn)
    conn.execute("INSERT INTO alerts (key, coin, criterion, severity, message, first_ts, surfaced_ts) "
                 "VALUES (?, 'X', 'bot', 'alto', 'm', 't', 't')", (f"bot:{bid}:1:stop:0",))
    conn.commit()
    assert botstore.delete(conn, bid) is True
    assert botstore.get(conn, twin)["twin_of"] is None
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE key LIKE ?", (f"bot:{bid}:%",)).fetchone()[0] == 0


def test_panel_deletes_stopped_bots_one_by_one_or_all_with_confirmation(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    ids = [botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="sim") for _ in range(3)]
    conn.execute("UPDATE bots SET status = 'stopped' WHERE id IN (?, ?)", ids[:2])
    conn.commit()
    tok = csrf(c, "/bots")
    page = c.get("/bots").get_data(as_text=True)
    assert "Apagar os bots parados" in page and "2 bots parados" in page and page.count("Apagar</a>") == 2
    assert "Apagar bot…" in c.get(f"/bots/{ids[0]}").get_data(as_text=True)
    assert "Apagar bot…" not in c.get(f"/bots/{ids[2]}").get_data(as_text=True)      # o que trabalha não tem o botão
    confirm = c.get(f"/bots/{ids[0]}/apagar").get_data(as_text=True)
    assert "Apagar 1 bot parado" in confirm and "Não se pode desfazer" in confirm
    assert botstore.get(conn, ids[0]) is not None                                   # ver a página não apaga
    c.post(f"/bots/{ids[0]}/apagar", data={"csrf": tok})
    assert botstore.get(conn, ids[0]) is None and botstore.get(conn, ids[1]) is not None
    r = c.post(f"/bots/{ids[2]}/apagar", data={"csrf": tok}, follow_redirects=True)     # a trabalhar: recusa
    assert botstore.get(conn, ids[2]) is not None and "Só se apaga um bot parado" in r.get_data(as_text=True)
    assert "Apagar 1 bot parado" in c.get("/bots/apagar-parados").get_data(as_text=True)
    c.post("/bots/apagar-parados", data={"csrf": tok})
    assert [r["id"] for r in botstore.all_bots(conn)] == [ids[2]]
    assert "Bots parados apagados" in [r["action"] for r in db.recent_log(conn, 5)]


def test_deleting_warns_when_the_bot_still_holds_coin(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {"never_sell_below_cost": True}, mode="testnet")
    for k in (1, 2):
        tick(conn, ex, bid, k)
    eng = engine(conn, bid)
    eng.command("stop", T0, close=50.0)                                             # abaixo do custo: fica com a moeda
    botstore.save_engine(conn, eng)
    assert eng.status == "stopped" and eng.s["base"] > 0
    tick(conn, ex, bid, 3)                                                          # o corredor cancela o que ficou aberto
    assert botstore.can_delete(conn, botstore.get(conn, bid))
    assert "ainda tem" in c.get(f"/bots/{bid}/apagar").get_data(as_text=True)


# ---------- bots em separadores: real, simulação e testnet ----------
def test_bots_are_split_into_real_simulation_and_testnet_tabs(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    sim = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="sim")
    tn = botstore.create(conn, "BTCUSDT", 50.0, {**ex.rules}, {}, mode="testnet")
    page = c.get("/bots").get_data(as_text=True)
    assert 'aria-label="Tipo de bot"' in page
    assert "Real <span" in page and "Simulação <span" in page and "Testnet <span" in page
    assert "BTCUSDT" in page and "XYZUSDT" not in page                              # por defeito abre na Testnet (há bots lá)
    assert 'aria-current="true">Testnet' in page
    simulation = c.get("/bots?aba=simulacao").get_data(as_text=True)
    assert "XYZUSDT" in simulation and "BTCUSDT" not in simulation and "Criar bot (simulação)" in simulation
    real = c.get("/bots?aba=real").get_data(as_text=True)
    assert "Ainda não disponível" in real and "só opera em <b>simulação</b>" in real
    assert "XYZUSDT" not in real and "BTCUSDT" not in real and "Criar bot" not in real   # nunca se cria dinheiro real aqui
    assert c.get("/bots?aba=lixo").status_code == 200
    only_sim = botstore.delete(conn, tn)                                            # (a trabalhar: não se apaga)
    assert only_sim is False and sim


def test_deleting_stopped_bots_only_touches_the_current_tab(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    sim = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="sim")
    tn = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    conn.execute("UPDATE bots SET status = 'stopped'")
    conn.commit()
    tok = csrf(c, "/bots")
    page = c.get("/bots?aba=simulacao").get_data(as_text=True)
    assert "aba=simulacao" in page and "Apagar os bots parados" in page
    assert "Apagar 1 bot parado" in c.get("/bots/apagar-parados?aba=simulacao").get_data(as_text=True)
    r = c.post("/bots/apagar-parados", data={"csrf": tok, "aba": "simulacao"})
    assert r.headers["Location"].endswith("aba=simulacao")
    assert botstore.get(conn, sim) is None and botstore.get(conn, tn) is not None   # o da Testnet ficou
    c.post(f"/bots/{tn}/apagar", data={"csrf": tok})
    assert botstore.get(conn, tn) is None
