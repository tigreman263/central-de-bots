"""Critérios da v0.1 (docs/DESIGN.md):
- sem palavra-passe não se vê nada;
- a configuração sobrevive a reiniciar;
- mostra preços da Binance em USDT/EUR/USD (aqui com preços de teste, sem rede);
- o botão de paragem geral está sempre visível.
"""
import re

import pytest

from app import create_app, market

PASSWORD = "uma-palavra-passe-boa"
FAKE_PRICES = {"BTCUSDT": 65000.0, "ETHUSDT": 3200.0, "EURUSDT": 1.10}


def make_app(tmp_path, fetch=lambda s: FAKE_PRICES):
    market.clear_cache()
    return create_app({"DATA_DIR": str(tmp_path), "MARKET_FETCH": fetch, "TESTING": True})


def csrf_of(client, url="/login"):
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def setup_and_login(client):
    token = csrf_of(client, "/setup")
    client.post("/setup", data={"csrf": token, "password": PASSWORD, "password2": PASSWORD})
    return client


@pytest.fixture
def client(tmp_path):
    return setup_and_login(make_app(tmp_path).test_client())


def test_nothing_visible_without_password(tmp_path):
    c = make_app(tmp_path).test_client()
    setup_and_login(c)
    c.post("/sair", data={"csrf": csrf_of(c, "/")})
    for url in ["/", "/configuracao", "/alertas", "/bots", "/parar-tudo"]:
        r = c.get(url)
        assert r.status_code == 302 and "/login" in r.headers["Location"], url


def test_first_use_forces_password_creation(tmp_path):
    c = make_app(tmp_path).test_client()
    r = c.get("/")
    assert r.status_code == 302 and "/setup" in r.headers["Location"]


def test_wrong_password_rejected_and_locked_after_five(tmp_path):
    c = make_app(tmp_path).test_client()
    setup_and_login(c)
    c.post("/sair", data={"csrf": csrf_of(c, "/")})
    token = csrf_of(c)
    for _ in range(5):
        r = c.post("/login", data={"csrf": token, "password": "errada"})
        assert "Palavra-passe errada" in r.get_data(as_text=True)
    r = c.post("/login", data={"csrf": token, "password": PASSWORD})
    assert "Demasiadas tentativas" in r.get_data(as_text=True)


def test_post_without_csrf_is_rejected(client):
    assert client.post("/parar-tudo", data={}).status_code == 400


def test_config_survives_restart(tmp_path):
    app1 = make_app(tmp_path)
    c = setup_and_login(app1.test_client())
    token = csrf_of(c, "/configuracao")
    form = {"csrf": token, "step": "save", "capital_eur": "560", "split_reserve": "50",
            "split_trading": "40", "split_cash": "10", "max_loss_trade": "1.5",
            "profit_to_reserve": "40", "pause_drawdown": "15", "exclusions": ["memecoins"]}
    r = c.post("/configuracao", data=form)
    assert r.status_code == 302

    app2 = make_app(tmp_path)  # "reiniciar o Pi": nova app, mesma base de dados
    c2 = app2.test_client()
    token = csrf_of(c2)
    c2.post("/login", data={"csrf": token, "password": PASSWORD})
    html = c2.get("/configuracao").get_data(as_text=True)
    assert 'name="split_reserve" inputmode="decimal" value="50"' in html
    assert 'name="max_loss_trade" inputmode="decimal" value="1.5"' in html
    assert 'value="memecoins" checked' in html
    assert 'value="leveraged" checked' not in html


def test_split_must_add_to_100(client):
    token = csrf_of(client, "/configuracao")
    form = {"csrf": token, "step": "save", "capital_eur": "560", "split_reserve": "50",
            "split_trading": "30", "split_cash": "10", "max_loss_trade": "2",
            "profit_to_reserve": "50", "pause_drawdown": "20"}
    html = client.post("/configuracao", data=form).get_data(as_text=True)
    assert "tem de dar 100%" in html
    assert 'name="split_reserve" inputmode="decimal" value="60"' in client.get("/configuracao").get_data(as_text=True)


def test_preview_shows_before_after_and_saves_nothing(client):
    token = csrf_of(client, "/configuracao")
    form = {"csrf": token, "step": "preview", "capital_eur": "560", "split_reserve": "60",
            "split_trading": "30", "split_cash": "10", "max_loss_trade": "3",
            "profit_to_reserve": "50", "pause_drawdown": "20", "exclusions": ["memecoins", "leveraged"]}
    html = client.post("/configuracao", data=form).get_data(as_text=True)
    assert "Perda máxima por operação" in html and "<b>3</b>" in html
    assert 'name="max_loss_trade" inputmode="decimal" value="2"' in client.get("/configuracao").get_data(as_text=True)


def test_prices_shown_and_converted(client):
    html = client.get("/").get_data(as_text=True)
    assert "65 000,00" in html                      # BTC em USDT
    assert f"{65000 / 1.10:,.2f}".replace(",", " ").replace(".", ",") in html  # BTC em EUR
    assert "USDT" in html and "EUR" in html and "USD" in html
    for cur, expect in [("USDT", "616,00 USDT"), ("EUR", "560,00 EUR"), ("USD", "616,00 USD")]:
        client.get(f"/moeda/{cur}")
        assert expect in client.get("/").get_data(as_text=True), cur


def test_no_invented_prices_when_binance_is_down(tmp_path):
    def broken(_symbols):
        raise OSError("sem rede")
    c = setup_and_login(make_app(tmp_path, fetch=broken).test_client())
    html = c.get("/").get_data(as_text=True)
    assert "Sem ligação à Binance" in html and "65" not in html.split("Preços reais")[1]


def test_stop_all_button_on_every_page_and_state_persists(client):
    for url in ["/", "/bots", "/reserva", "/estatisticas", "/alertas", "/configuracao"]:
        assert "PARAR TUDO" in client.get(url).get_data(as_text=True), url
    assert "cancela as ordens abertas" in client.get("/parar-tudo").get_data(as_text=True)
    client.post("/parar-tudo", data={"csrf": csrf_of(client, "/parar-tudo")})
    html = client.get("/").get_data(as_text=True)
    assert "SISTEMA PARADO" in html and "PARAR TUDO" not in html
    assert "PARAR TUDO" in client.get("/alertas").get_data(as_text=True)  # entrada no registo
    client.post("/reativar", data={"csrf": csrf_of(client, "/reativar")})
    assert "PARAR TUDO" in client.get("/").get_data(as_text=True)


def test_simulation_banner_always_visible(client, tmp_path):
    assert "MODO SIMULAÇÃO" in client.get("/").get_data(as_text=True)
    assert "MODO SIMULAÇÃO" in make_app(tmp_path).test_client().get("/login").get_data(as_text=True)
    assert "MODO SIMULAÇÃO" in make_app(tmp_path / "novo").test_client().get("/setup").get_data(as_text=True)


def test_config_shows_recommendations_for_every_field(client):
    html = client.get("/configuracao").get_data(as_text=True)
    assert html.count('<details class="recs">') == 12  # 6 campos + 6 limiares de risco
    for profile in ("Conservador", "Equilibrado", "Arrojado"):
        assert profile in html
    assert "Erro comum" in html and "Com pouco capital" in html
    assert "nunca atives levantamentos" in html
    assert "pontos de partida a validar em simulação" in html
    assert html.count("data-apply=") >= 12  # botões "Aplicar" dos perfis numéricos e das categorias


def test_apply_buttons_only_fill_the_form_and_never_save(client):
    html = client.get("/configuracao").get_data(as_text=True)
    assert 'type="button" class="btn small"' in html
    cfg = client.get("/configuracao").get_data(as_text=True)
    assert 'name="split_reserve" inputmode="decimal" value="60"' in cfg


def test_every_risk_threshold_explains_itself_and_has_valid_profiles(client):
    from werkzeug.datastructures import MultiDict
    from app import db, recommendations as rec
    from app.rules import parse_config
    html = client.get("/configuracao").get_data(as_text=True)
    assert len(rec.RISK_ITEMS) == 6
    for it in rec.RISK_ITEMS:
        assert it["title"] in html and it["what"].split(".")[0] in html          # o que é, em linguagem simples
        assert f'id="{it["attn_key"]}"' in html and f'id="{it["high_key"]}"' in html   # o botão Aplicar precisa dos ids
        assert it["attn_label"] in html and it["high_label"] in html                 # etiquetas visíveis, não só valores
        assert "Erro comum: " + it["mistake"].replace('"', "&#34;") in html or it["mistake"][:30] in html
        assert [p[0] for p in it["profiles"]] == ["Conservador", "Equilibrado", "Arrojado"]
        for _name, _text, attn, high in it["profiles"]:                             # cada perfil passa a validação real
            form = {"split_reserve": "60", "split_trading": "30", "split_cash": "10", "capital_eur": "560",
                    "max_loss_trade": "2", "profit_to_reserve": "50", "pause_drawdown": "20",
                    it["attn_key"]: str(attn), it["high_key"]: str(high)}
            clean, errors = parse_config(MultiDict(form))
            assert not errors, (it["key"], errors)
    assert html.count("data-apply=") >= 12 + 18                                   # campos antigos + 6 x 3 perfis
    assert "Nada aqui compra ou vende" in html and "atenção</b> (vê isto)" in html
