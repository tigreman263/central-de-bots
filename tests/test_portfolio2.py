"""Critérios de docs/PORTFOLIO2.md: custo médio, Portefólio reorganizado, Staking e Ai."""
import json
import re

from app import analysis, db, layout, staking
from test_portfolio import API_KEY, SECRET_KEY, csrf, env, klines, save_key  # noqa: F401


def snapshot(app):
    return json.loads(db.get(db.connect(app.config["DB_PATH"]), "portfolio_snapshot"))


def set_cost(c, coin, price):
    return c.post("/portefolio/custo", data={"csrf": csrf(c, "/portefolio"), "coin": coin, "price": price},
                  follow_redirects=True).get_data(as_text=True)


def add_coin(world, coin, price, qty, volume="90000000"):
    world.balances += [(coin, qty, "0")]
    world.tickers[f"{coin}USDT"] = {"lastPrice": price, "priceChangePercent": "-2", "quoteVolume": volume,
                                    "bidPrice": str(float(price) * 0.9999), "askPrice": str(float(price) * 1.0001)}
    world.klines[f"{coin}USDT"] = klines()
    world.status[f"{coin}USDT"] = "TRADING"


# ---------- custo médio ----------
def test_cost_basis_save_edit_delete_and_validation(env):
    c, world, app, tmp = env
    add_coin(world, "ADA", "0.50", "100")
    save_key(c)
    assert "sem custo" in c.get("/portefolio").get_data(as_text=True)
    assert "guardado: 0.75 USDT" in set_cost(c, "ada", "0,75")
    assert "guardado: 0.8 USDT" in set_cost(c, "ADA", "0.8")            # editar
    assert "preço médio maior que zero" in set_cost(c, "ADA", "-1")
    assert "preço médio maior que zero" in set_cost(c, "ADA", "abc")
    html = c.post("/portefolio/custo", data={"csrf": csrf(c, "/portefolio"), "coin": "ADA", "action": "delete"},
                  follow_redirects=True).get_data(as_text=True)
    assert "removido" in html


def test_ada_at_075_below_price_shows_loss_and_warning_and_never_invents_cost(env):
    c, world, app, tmp = env
    add_coin(world, "ADA", "0.50", "100")        # 100 ADA a 0,50 vs custo 0,75 => -25 USDT
    save_key(c)
    set_cost(c, "ADA", "0,75")
    html = c.get("/portefolio").get_data(as_text=True)
    assert "Vender ADA agora fixa um prejuízo de cerca de 25.05 USDT" in html   # 25 + 0,1% de 50 USDT em comissões
    assert "custo 0,75" in html or "custo 0.75" in html
    assert "−25,00" in html
    btc_row = html.split('id="coin-BTC"')[1].split("</tr>")[0]
    assert "sem custo" in btc_row                # BTC não tem custo: nada inventado


def test_price_above_cost_shows_profit_without_warning(env):
    c, world, app, tmp = env
    add_coin(world, "ADA", "1.00", "100")
    save_key(c)
    set_cost(c, "ADA", "0.75")
    html = c.get("/portefolio").get_data(as_text=True)
    assert "Vender ADA agora" not in html and "+25,00" in html


# ---------- Portefólio reorganizado ----------
def test_sections_add_up_to_total_and_dust_is_counted_but_hidden(env):
    c, world, app, tmp = env
    world.balances += [("EUR", "20", "0")]
    add_coin(world, "DOGE", "0.1", "5")          # 0,5 USDT => poeira
    save_key(c)
    snap = snapshot(app)
    view = layout.build(snap, snap["holdings"], 1.0, "valor", "", True, [])
    total = sum(r["value_usdt"] or 0 for s in view["sections"] for r in s["rows"])
    assert abs(total - snap["total_usdt"]) < 1e-6
    hidden = c.get("/portefolio").get_data(as_text=True)
    assert "Escondida" in hidden and 'id="coin-DOGE"' not in hidden
    assert 'id="coin-DOGE"' in c.get("/portefolio?po=1").get_data(as_text=True)
    assert "Stablecoins e moeda" in hidden and "Moedas principais" in hidden


def test_earn_dominant_coins_go_to_earn_section(env):
    c, world, app, tmp = env
    world.balances = [("LDBTC", "0.005", "0"), ("USDT", "100", "0")]
    save_key(c)
    html = c.get("/portefolio").get_data(as_text=True)
    assert 'id="sec-earn"' in html and 'id="sec-principais"' not in html


def test_risk_badge_on_the_right_coin_and_filters_and_sort_work(env):
    c, world, app, tmp = env
    add_coin(world, "ADA", "0.50", "100")
    world.tickers["BTCUSDT"]["quoteVolume"] = "100"     # só o BTC fica com risco de liquidez
    save_key(c)
    html = c.get("/portefolio").get_data(as_text=True)
    btc_row = html.split('id="coin-BTC"')[1].split("</tr>")[0]
    ada_row = html.split('id="coin-ADA"')[1].split("</tr>")[0]
    assert "Liquidez" in btc_row and "Liquidez" not in ada_row
    only = c.get("/portefolio?filtro=alertas").get_data(as_text=True)
    assert 'id="coin-BTC"' in only and 'id="coin-ADA"' not in only
    by_name = c.get("/portefolio?ordem=nome").get_data(as_text=True)
    assert by_name.index('id="coin-ADA"') < by_name.index('id="coin-BTC"')


# ---------- Staking ----------
def earn_world(world):
    world.earn_flex_pos = [{"asset": "USDT", "totalAmount": "40", "latestAnnualPercentageRate": "0.03",
                            "canRedeem": True, "yesterdayRealTimeRewards": "0.003", "cumulativeTotalRewards": "1"}]
    world.earn_locked_pos = [{"asset": "BTC", "amount": "0.001", "apy": "0.05", "duration": 30,
                              "deliverDate": 1893456000000, "canRedeemEarly": False, "autoSubscribe": False,
                              "status": "HOLDING"}]
    world.earn_flex_prod = [{"asset": "BTC", "latestAnnualPercentageRate": "0.01", "canPurchase": True,
                             "isSoldOut": False, "minPurchaseAmount": "0.0001"}]
    world.earn_locked_prod = [{"projectId": "p1", "quota": {"minimum": "0.0001"},
                               "detail": {"asset": "BTC", "duration": 60, "apr": "0.04", "status": "PURCHASING",
                                          "isSoldOut": False, "renewable": True}}]


def test_staking_reads_positions_and_suggests_with_reasons(env):
    c, world, app, tmp = env
    earn_world(world)
    save_key(c)
    html = c.get("/staking").get_data(as_text=True)
    assert "Flexível" in html and "Bloqueado" in html
    assert "Sugestões para o teu saldo parado" in html
    assert "Na Binance: Earn" in html and "A decisão é tua" in html and "não é garantido" in html
    assert "3.00% APR" in html or "5.00%" in html          # APR das posições lidas
    assert "podes resgatar quando quiseres" in html or "só o resgatas no fim do prazo" in html


def test_staking_never_suggests_locked_for_coin_with_risk_alert(env):
    c, world, app, tmp = env
    earn_world(world)
    world.tickers["BTCUSDT"]["quoteVolume"] = "100"      # alerta de liquidez no BTC
    save_key(c)
    html = c.get("/staking").get_data(as_text=True)
    sug_part = html.split("Sugestões para o teu saldo parado")[1].split("Avisos:")[0]
    assert "Bloqueado" not in sug_part and "Flexível" in sug_part


def test_staking_locked_suggestion_limited_to_half_and_ignores_dust():
    snap = {"flexible": [], "locked": [], "flex_products": {},
            "locked_products": {"XYZ": [{"duration": 30, "apr": 8.0, "min": 0.1, "renewable": False}]}}
    h = [{"coin": "XYZ", "priced": True, "price": 2.0, "qty": 10.0, "earn": 0.0, "locked": 0.0}]
    s = staking.suggestions(snap, h, {})
    assert s[0]["kind"] == "Bloqueado" and s[0]["share"] == 50.0 and abs(s[0]["amount"] - 5.0) < 1e-9
    assert staking.suggestions(snap, h, {"XYZ": "atenção"}) == []          # moeda com alerta: nada bloqueado
    tiny = [{"coin": "XYZ", "priced": True, "price": 0.1, "qty": 10.0, "earn": 0.0, "locked": 0.0}]
    assert staking.suggestions(snap, tiny, {}) == []                       # 1 USDT parado: não vale a pena


def test_staking_failure_shows_unavailable_and_marks_old_data(env):
    c, world, app, tmp = env
    earn_world(world)
    save_key(c)
    c.get("/staking")
    world.fail = "Sem ligação à Binance."
    html = c.post("/staking/atualizar", data={"csrf": csrf(c, "/staking")}, follow_redirects=True).get_data(as_text=True)
    assert "Sem ligação ao Earn: dados indisponíveis" in html and "desatualizada" in html


# ---------- Ai ----------
def test_ai_page_has_blocks_tips_loss_and_disabled_chat(env):
    c, world, app, tmp = env
    add_coin(world, "ADA", "0.50", "100")
    earn_world(world)
    save_key(c)
    set_cost(c, "ADA", "0.75")
    html = c.get("/ai").get_data(as_text=True)
    for block in ("Desempenho", "Concentração", "Risco", "Diversificação", "Staking", "Lucro/prejuízo por moeda"):
        assert block in html, block
    assert "ADA está 33.3% abaixo do teu custo médio" in html
    assert "A decisão é sua" in html and "Número:" in html
    assert 'placeholder="Pergunta sobre a tua carteira' in html and "Ainda não ligada" in html


def test_ai_summary_has_no_secrets_and_verifier_catches_leaks(env):
    c, world, app, tmp = env
    add_coin(world, "ADA", "0.50", "100")
    save_key(c)
    set_cost(c, "ADA", "0.75")
    html = c.get("/ai").get_data(as_text=True)
    assert API_KEY not in html and SECRET_KEY not in html
    body = re.search(r'<pre class="jsonbox">(.*?)</pre>', html, re.S).group(1)
    assert "version" in body and "holdings" in body and "Read-only summary" in body
    assert analysis.verify_no_secrets("x " + API_KEY, [API_KEY])
    assert analysis.verify_no_secrets("a" * 64, [])
    assert analysis.verify_no_secrets('{"coin": "BTC"}', [API_KEY]) == []


def test_ai_summary_is_blocked_if_it_would_leak_a_secret(env, monkeypatch):
    c, world, app, tmp = env
    save_key(c)
    monkeypatch.setattr(analysis, "summary_text", lambda s: "leak " + API_KEY)
    html = c.get("/ai").get_data(as_text=True)
    assert "O resumo foi bloqueado" in html and API_KEY not in html


def test_new_tabs_require_login_and_keep_stop_button(env):
    c, world, app, tmp = env
    for url in ["/staking", "/ai", "/portefolio"]:
        assert "PARAR TUDO" in c.get(url).get_data(as_text=True), url
    anon = app.test_client()
    for url in ["/staking", "/ai", "/portefolio"]:
        assert anon.get(url).status_code == 302, url


def test_dust_creates_no_risk_alerts_and_is_aggregated_in_ai_summary(env):
    c, world, app, tmp = env
    add_coin(world, "DOGE", "0.1", "5", volume="100")       # 0,5 USDT e pouca liquidez: poeira
    save_key(c)
    assert "DOGE:liquidez" not in c.get("/alertas").get_data(as_text=True) and "DOGE:" not in c.get("/alertas").get_data(as_text=True)
    html = c.get("/ai").get_data(as_text=True)
    body = re.search(r'<pre class="jsonbox">(.*?)</pre>', html, re.S).group(1)
    assert "DOGE" not in body and "dust" in body
