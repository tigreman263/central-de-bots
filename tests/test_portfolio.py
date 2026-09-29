"""Critérios do Portefólio (docs/PORTFOLIO.md), com respostas simuladas da Binance."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import alerts, create_app, db, market, risk
from app.reader import BinanceError, check_permissions

SECRET_KEY = "SEGREDO-DE-TESTE-abcdef123456"
API_KEY = "CHAVE-DE-TESTE-xyz9876"
GOOD = {"enableReading": True, "ipRestrict": True}


def klines(n=90, open_=100.0, close=100.0):
    return [[i, str(open_), "0", "0", str(close), "0"] for i in range(n)]


class World:
    """Estado falso da Binance, editável por cada teste."""

    def __init__(self):
        self.restrictions = dict(GOOD)
        self.balances = [("BTC", "0.005", "0"), ("USDT", "100", "0")]
        self.fail = None
        self.tickers = {
            "EURUSDT": {"lastPrice": "1.10"},
            "BTCUSDT": {"lastPrice": "60000", "priceChangePercent": "1.5", "quoteVolume": "500000000",
                        "bidPrice": "59999", "askPrice": "60001"},
        }
        self.klines = {"BTCUSDT": klines()}
        self.status = {"BTCUSDT": "TRADING"}
        self.earn_flex_pos, self.earn_locked_pos = [], []
        self.earn_flex_prod, self.earn_locked_prod = [], []


class FakeReader:
    def __init__(self, world):
        self.w = world

    def _check(self):
        if self.w.fail:
            raise BinanceError(self.w.fail)

    def account(self):
        self._check()
        return [{"asset": a, "free": f, "locked": l} for a, f, l in self.w.balances]

    def restrictions(self):
        self._check()
        return self.w.restrictions

    def ticker24(self, symbol):
        self._check()
        if symbol not in self.w.tickers:
            raise BinanceError("símbolo desconhecido")
        return self.w.tickers[symbol]

    def tickers_all(self):
        self._check()
        return {k: {"symbol": k, **v} for k, v in self.w.tickers.items()}

    def earn_flexible_positions(self):
        self._check()
        return self.w.earn_flex_pos

    def earn_locked_positions(self):
        self._check()
        return self.w.earn_locked_pos

    def earn_flexible_products(self):
        self._check()
        return self.w.earn_flex_prod

    def earn_locked_products(self):
        self._check()
        return self.w.earn_locked_prod

    def klines(self, symbol, limit=90):
        return self.w.klines[symbol]

    def symbol_status(self, symbol):
        return self.w.status.get(symbol)


@pytest.fixture
def env(tmp_path):
    market.clear_cache()
    world = World()
    app = create_app({
        "DATA_DIR": str(tmp_path / "data"), "KEY_FILE": str(tmp_path / "keys" / "k.json"),
        "MARKET_FETCH": lambda s: {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "EURUSDT": 1.1},
        "READER_FACTORY": lambda key, secret: FakeReader(world), "TESTING": True,
    })
    c = app.test_client()
    tok = csrf(c, "/setup")
    c.post("/setup", data={"csrf": tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    return c, world, app, tmp_path


def csrf(c, url):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def save_key(c, key=API_KEY, secret=SECRET_KEY):
    return c.post("/portefolio/chave", data={"csrf": csrf(c, "/portefolio/chave"), "action": "save",
                                             "api_key": key, "api_secret": secret}, follow_redirects=True)


# ---------- chave ----------
def test_key_with_trading_or_withdrawals_is_refused_and_not_saved(env):
    c, world, app, tmp = env
    for flag in ("enableWithdrawals", "enableSpotAndMarginTrading", "enableMargin", "enableFutures"):
        world.restrictions = {**GOOD, flag: True}
        html = save_key(c).get_data(as_text=True)
        assert "Chave recusada" in html, flag
        assert not (tmp / "keys" / "k.json").exists(), flag
    world.restrictions = {"enableReading": False}
    assert "não tem permissão de leitura" in save_key(c).get_data(as_text=True)
    assert not (tmp / "keys" / "k.json").exists()


def test_read_only_key_is_saved_and_warns_without_ip_restriction(env):
    c, world, app, tmp = env
    world.restrictions = {"enableReading": True, "ipRestrict": False}
    html = save_key(c).get_data(as_text=True)
    assert "restrita a um IP" in html
    assert (tmp / "keys" / "k.json").exists()


def test_key_never_appears_in_db_pages_or_logs(env):
    c, world, app, tmp = env
    save_key(c)
    c.get("/portefolio")
    pages = "".join(c.get(u).get_data(as_text=True) for u in ["/portefolio", "/portefolio/chave", "/alertas", "/configuracao", "/"])
    assert API_KEY not in pages and SECRET_KEY not in pages
    assert "…" + API_KEY[-4:] in pages  # só os últimos 4 caracteres
    for f in (tmp / "data").rglob("*"):
        if f.is_file():
            assert API_KEY.encode() not in f.read_bytes() and SECRET_KEY.encode() not in f.read_bytes(), f
    project = Path(__file__).resolve().parent.parent
    assert not str(tmp / "keys").startswith(str(project))
    assert "data/" in (project / ".gitignore").read_text()


def test_bad_key_shows_portuguese_error_and_saves_nothing(env):
    c, world, app, tmp = env
    world.fail = "A Binance recusou a chave. Confirma a chave."
    assert "A Binance recusou a chave" in save_key(c).get_data(as_text=True)
    assert not (tmp / "keys" / "k.json").exists()


# ---------- leitura e valores ----------
def test_no_key_shows_call_to_action(env):
    c, *_ = env
    assert "Ligar chave da Binance" in c.get("/portefolio").get_data(as_text=True)


def test_values_and_weights_add_up_to_100(env):
    c, world, app, tmp = env
    world.balances = [("BTC", "0.005", "0.001"), ("USDT", "100", "0"), ("EUR", "50", "0")]
    save_key(c)
    snap = __import__("json").loads(db.get(db.connect(app.config["DB_PATH"]), "portfolio_snapshot"))
    assert abs(sum(h["weight"] for h in snap["holdings"]) - 100) < 1e-6
    expected = 0.006 * 60000 + 100 + 50 * 1.10
    assert abs(snap["total_usdt"] - expected) < 1e-6
    html = c.get("/portefolio").get_data(as_text=True)
    assert "BTC" in html and "USDT" in html and "em ordens" in html


def test_dust_is_hidden_unless_asked(env):
    c, world, app, tmp = env
    world.balances = [("BTC", "0.000001", "0"), ("USDT", "100", "0")]  # ~0,06 USDT
    save_key(c)
    assert "<b>BTC</b>" not in c.get("/portefolio").get_data(as_text=True)
    assert "<b>BTC</b>" in c.get("/portefolio?po=1").get_data(as_text=True)


# ---------- critérios de risco ----------
def hold(coin="XYZ", **kw):
    base = {"coin": coin, "priced": True, "stable": False, "weight": 5.0}
    base.update(kw)
    return base


T = risk.thresholds({})


@pytest.mark.parametrize("field,value,expect", [
    ("weight", 40.0, None), ("weight", 40.01, "atenção"), ("weight", 60.0, "atenção"), ("weight", 60.01, "alto"),
    ("vol30", 6.0, None), ("vol30", 6.01, "atenção"), ("vol30", 10.01, "alto"),
    ("quote_volume", 1_000_000, None), ("quote_volume", 999_999, "atenção"), ("quote_volume", 200_000, "atenção"),
    ("quote_volume", 199_999, "alto"),
    ("spread_pct", 0.3, None), ("spread_pct", 0.31, "atenção"), ("spread_pct", 1.01, "alto"),
    ("age_days", 90, None), ("age_days", 89, "info"), ("age_days", 30, "info"), ("age_days", 29, "atenção"),
])
def test_each_criterion_fires_at_the_right_level(field, value, expect):
    found = risk.evaluate([hold(**{field: value})], T)
    if expect is None:
        assert found == []
    else:
        assert len(found) == 1 and found[0]["severity"] == expect
        assert found[0]["message"].endswith("A decisão é sua.")


def test_stablecoin_deviation_and_status_and_concentration_ignores_stables():
    assert risk.evaluate([hold("USDC", stable=True, stable_dev_pct=0.5, weight=90)], T) == []
    assert risk.evaluate([hold("USDC", stable=True, stable_dev_pct=0.6, weight=90)], T)[0]["severity"] == "atenção"
    assert risk.evaluate([hold("USDC", stable=True, stable_dev_pct=2.5)], T)[0]["severity"] == "alto"
    f = risk.evaluate([hold(status="BREAK")], T)
    assert f[0]["criterion"] == "estado" and f[0]["severity"] == "alto"
    assert risk.evaluate([hold(status="TRADING")], T) == []
    assert risk.evaluate([{"coin": "ABC", "priced": False, "weight": 99, "stable": False}], T) == []  # sem preço: sem estimativa


# ---------- alertas ----------
@pytest.fixture
def conn(tmp_path):
    db.init_db(str(tmp_path / "a.db"))
    c = db.connect(str(tmp_path / "a.db"))
    alerts.init(c)
    return c


def finding(sev="atenção", key="BTC:concentracao"):
    return {"key": key, "coin": "BTC", "criterion": "concentracao", "severity": sev, "message": "BTC: x. A decisão é sua."}


def test_same_alert_not_repeated_within_24h_and_repeats_after(conn):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert alerts.sync(conn, [finding()], t0) == ["BTC:concentracao"]
    assert alerts.sync(conn, [finding()], t0 + timedelta(hours=23)) == []
    assert alerts.sync(conn, [finding()], t0 + timedelta(hours=25)) == ["BTC:concentracao"]


def test_seen_alert_stays_quiet_but_rise_in_severity_resurfaces(conn):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    alerts.sync(conn, [finding()], t0)
    alerts.mark_seen(conn, "BTC:concentracao")
    assert alerts.sync(conn, [finding()], t0 + timedelta(hours=48)) == []
    assert alerts.unseen_top(conn) is None
    assert alerts.sync(conn, [finding("alto")], t0 + timedelta(hours=49)) == ["BTC:concentracao"]
    assert alerts.unseen_top(conn) == "alto"


def test_info_alert_shows_only_once(conn):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    f = finding("info", "X:recente")
    assert alerts.sync(conn, [f], t0) == ["X:recente"]
    assert alerts.sync(conn, [f], t0 + timedelta(days=3)) == []


def test_alert_closes_itself_when_condition_disappears(conn):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    alerts.sync(conn, [finding()], t0)
    assert len(alerts.open_alerts(conn)) == 1
    alerts.sync(conn, [], t0 + timedelta(hours=1))
    assert alerts.open_alerts(conn) == []


# ---------- falhas de ligação ----------
def test_connection_failure_shows_unavailable_keeps_old_data_marked_and_suspends_analysis(env):
    c, world, app, tmp = env
    world.balances = [("BTC", "0.005", "0"), ("USDT", "100", "0")]
    world.tickers["BTCUSDT"]["quoteVolume"] = "100"  # dispara liquidez alta
    save_key(c)
    html = c.get("/portefolio").get_data(as_text=True)
    assert "Liquidez" in html and "alerta(s) de risco" in html  # selo na moeda; a frase completa fica em Alertas
    world.fail = "Sem ligação à Binance."
    world.balances = [("BTC", "9", "0")]  # se fosse lido, o valor mudava; não pode aparecer
    html = c.post("/portefolio/atualizar", data={"csrf": csrf(c, "/portefolio")}, follow_redirects=True).get_data(as_text=True)
    assert "Sem ligação: dados indisponíveis" in html and "desatualizados" in html and "antigo" in html
    assert "540" not in html  # 9 BTC * 60000 nunca aparece
    a = c.get("/alertas").get_data(as_text=True)
    assert "Ligação à Binance falhou" in a and "análises de risco ficam suspensas" in a
    # a análise não fechou os alertas antigos por falta de dados
    assert "negociou apenas" in a
    # a ligação volta: o alerta de ligação fecha
    world.fail = None
    world.balances = [("BTC", "0.005", "0"), ("USDT", "100", "0")]
    c.post("/portefolio/atualizar", data={"csrf": csrf(c, "/portefolio")})
    assert "Ligação à Binance falhou" not in c.get("/alertas").get_data(as_text=True)


def test_real_portfolio_replaces_estimated_capital_on_home(env):
    c, world, app, tmp = env
    assert "Capital simulado" in c.get("/").get_data(as_text=True)
    save_key(c)
    html = c.get("/").get_data(as_text=True)
    assert "Portefólio real" in html and "Capital simulado" not in html


# ---------- limiares configuráveis ----------
def test_risk_thresholds_are_configurable_and_validated(env):
    c, *_ = env
    base = {"csrf": csrf(c, "/configuracao"), "step": "preview", "capital_eur": "560", "split_reserve": "60",
            "split_trading": "30", "split_cash": "10", "max_loss_trade": "2", "profit_to_reserve": "50",
            "pause_drawdown": "20", "risk_conc_attn": "50", "risk_conc_high": "40"}
    assert "tem de ser maior" in c.post("/configuracao", data=base).get_data(as_text=True)
    base["risk_conc_high"] = "70"
    html = c.post("/configuracao", data=base).get_data(as_text=True)
    assert "Concentração de uma moeda: atenção" in html and "<b>50</b>" in html


# ---------- só leitura ----------
def test_no_order_sell_or_withdraw_code_exists():
    app_dir = Path(__file__).resolve().parent.parent / "app"
    # v0.3: ordens só em trader.py (e só na Testnet); todos os outros módulos continuam sem código de ordens
    source = "\n".join(p.read_text(encoding="utf-8") for p in app_dir.glob("*.py") if p.name != "trader.py")
    for forbidden in ["/api/v3/order", "/sapi/v1/capital/withdraw", "withdraw/apply", "/sapi/v1/asset/transfer",
                      'method="POST"', "method='POST'", "def place_order", "def sell", "def withdraw",
                      "simple-earn/flexible/subscribe", "simple-earn/locked/subscribe", "/redeem", "def subscribe"]:
        assert forbidden not in source, forbidden
    reader_src = (app_dir / "reader.py").read_text(encoding="utf-8")
    assert 'method="GET"' in reader_src


def test_permission_check_rules():
    ok, problems, _ = check_permissions({"enableReading": True})
    assert ok and not problems
    assert not check_permissions({"enableReading": True, "enableWithdrawals": True})[0]
    assert not check_permissions({"enableReading": False})[0]


def test_stop_all_still_works_on_portfolio_pages(env):
    c, *_ = env
    for url in ["/portefolio", "/portefolio/chave", "/alertas", "/mais"]:
        assert "PARAR TUDO" in c.get(url).get_data(as_text=True), url
    c.post("/parar-tudo", data={"csrf": csrf(c, "/parar-tudo")})
    assert "SISTEMA PARADO" in c.get("/portefolio").get_data(as_text=True)


def test_binance_earn_ld_assets_are_merged_into_the_base_coin(env):
    c, world, app, tmp = env
    world.balances = [("BTC", "0.001", "0"), ("LDBTC", "0.004", "0"), ("USDT", "100", "0"), ("LDXYZ", "5", "0")]
    save_key(c)
    snap = __import__("json").loads(db.get(db.connect(app.config["DB_PATH"]), "portfolio_snapshot"))
    coins = {h["coin"]: h for h in snap["holdings"]}
    assert abs(coins["BTC"]["qty"] - 0.005) < 1e-12 and abs(coins["BTC"]["earn"] - 0.004) < 1e-12
    assert abs(snap["total_usdt"] - (0.005 * 60000 + 100)) < 1e-6  # o Earn conta para o total
    assert "LDBTC" not in coins
    assert coins["LDXYZ"]["priced"] is False  # LD sem moeda base conhecida: continua "sem preço", sem estimativa
    assert "no Earn" in c.get("/portefolio").get_data(as_text=True)


def test_a_leftover_snapshot_without_the_key_still_present_is_never_shown_as_current(env):
    """Achado real de uso: a chave real foi apagada por fora do botão "remover" (ficheiro apagado à mão), mas o
    snapshot antigo ficou na base de dados — o Início continuava a mostrar "Portefólio real" com dados de dias
    antes, em vez de cair para o capital simulado de Configuração."""
    c, world, app, tmp = env
    conn = db.connect(app.config["DB_PATH"])
    db.set_many(conn, {"portfolio_snapshot": '{"total_usdt": 999.0, "eur_per_usdt": 1.1, "ts": "2020-01-01T00:00:00+00:00", "holdings": []}'})
    html = c.get("/").get_data(as_text=True)
    assert "Portefólio real" not in html and "Capital simulado" in html
