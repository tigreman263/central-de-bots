"""v0.2 ponta a ponta: escolha do par, painel, corredor, comandos, retoma e 7 dias sem intervenção (sintético)."""
import json
import re

import pytest

from app import botstore, create_app, db, market, pairs, runner
from app.engine import MIN, PAUSED, RUNNING, STOPPED, Engine
from app.reader import BinanceError
from test_grid import RULES, flat, line, sine
from test_grid import candles as _candles

T0 = 1_699_999_200_000   # instante realista (múltiplo de 1 hora); o corredor usa 0 para "ainda sem velas"


def candles(path, **kw):
    return _candles(path, start=T0, **kw)

PAIR = "ABCUSDT"


def ticker(vol="90000000", bid="99.99", ask="100.01", high="103", low="98", last="100"):
    return {"symbol": PAIR, "lastPrice": last, "highPrice": high, "lowPrice": low, "bidPrice": bid, "askPrice": ask,
            "quoteVolume": vol, "priceChangePercent": "1"}


class FakeMarket:
    """Binance falsa: velas de 1 minuto sintéticas e regras do par."""

    def __init__(self, cs=None):
        self.cs = cs or []
        self.fail = None
        self.now = None                       # relógio do teste: a Binance só devolve velas já fechadas até agora
        self.tickers = {PAIR: ticker()}

    def _check(self):
        if self.fail:
            raise BinanceError(self.fail)

    def tickers_all(self):
        self._check()
        return self.tickers

    def ticker24(self, symbol):
        self._check()
        return {"lastPrice": str(self.cs[-1][4]) if self.cs else "100"}

    def symbol_rules(self, symbol):
        self._check()
        return {**RULES, "status": "TRADING"}

    def klines_1m(self, symbol, start_ms=None, limit=1000):
        self._check()
        closed = [c for c in self.cs if self.now is None or c[0] + MIN <= self.now]
        rows = [c for c in closed if start_ms is None or c[0] >= start_ms]
        rows = rows[-limit:] if start_ms is None else rows[:limit]
        return [[c[0], str(c[1]), str(c[2]), str(c[3]), str(c[4])] for c in rows]

    def account(self):
        return []


def tick(conn, fm, now_ms):
    fm.now = now_ms
    runner.tick_all(conn, fm, now_ms=now_ms)


# ---------- escolha do par ----------
def test_pair_filters_and_ordering():
    t = {
        "GOODUSDT": {**ticker(), "symbol": "GOODUSDT"},
        "DOGEUSDT": {**ticker(), "symbol": "DOGEUSDT"},                                  # memecoin
        "BTCUPUSDT": {**ticker(), "symbol": "BTCUPUSDT"},                                # alavancada
        "USDCUSDT": {**ticker(), "symbol": "USDCUSDT"},                                  # stablecoin
        "THINUSDT": {**ticker(vol="1000000"), "symbol": "THINUSDT"},                     # pouca liquidez
        "WIDEUSDT": {**ticker(bid="99", ask="101"), "symbol": "WIDEUSDT"},               # spread largo
        "DEADUSDT": {**ticker(high="100.5", low="99.6"), "symbol": "DEADUSDT"},          # parado
        "WILDUSDT": {**ticker(high="125", low="90"), "symbol": "WILDUSDT"},              # louco
        "GOODBTC": {**ticker(), "symbol": "GOODBTC"},                                    # não é USDT
    }
    out = pairs.suggest(t)
    assert [p["pair"] for p in out] == ["GOODUSDT"]
    assert pairs.suggest(t, exclusions=())[0]["pair"] in {"GOODUSDT", "DOGEUSDT", "BTCUPUSDT", "USDCUSDT"}
    assert all("M USDT" in p["why"][0] for p in out)


# ---------- painel ----------
@pytest.fixture
def env(tmp_path):
    market.clear_cache()
    fm = FakeMarket(candles(flat(5)))
    app = create_app({"DATA_DIR": str(tmp_path / "data"), "KEY_FILE": str(tmp_path / "k.json"),
                      "MARKET_FETCH": lambda s: {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "EURUSDT": 1.1},
                      "READER_FACTORY": lambda k, s: fm, "TESTING": True})
    c = app.test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/setup").get_data(as_text=True)).group(1)
    c.post("/setup", data={"csrf": tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    return c, fm, app


def tok(c, url):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def conn_of(app):
    return db.connect(app.config["DB_PATH"])


def test_creating_a_bot_needs_preview_then_explicit_approval(env):
    c, fm, app = env
    html = c.get("/bots/novo").get_data(as_text=True)
    assert PAIR in html and "liquidez alta" in html and "A explicação é escrita pelo código" in html
    form = {"csrf": tok(c, "/bots/novo"), "pair": PAIR, "capital": "77", "step": "preview"}
    prev = c.post("/bots/novo", data=form).get_data(as_text=True)
    assert "Aprovar bot ABCUSDT" in prev and "Stop-loss" in prev and "reserva de caixa" in prev
    assert botstore.all_bots(conn_of(app)) == []                          # ver a grelha não cria nada
    r = c.post("/bots/novo", data={**form, "step": "create"})
    assert r.status_code == 302
    bots = botstore.all_bots(conn_of(app))
    assert len(bots) == 1 and bots[0]["status"] == "pending" and bots[0]["capital_usdt"] == 77


def test_creating_a_bot_with_an_unknown_mode_falls_back_to_simulation_never_real(env):
    """A conta real ainda não tem o seu próprio fluxo de criação de bots — "real" nunca pode passar aqui,
    nem por um valor de formulário inesperado ou manipulado."""
    c, fm, app = env
    form = {"csrf": tok(c, "/bots/novo?modo=real"), "pair": PAIR, "capital": "77", "step": "create", "modo": "real"}
    c.post("/bots/novo?modo=real", data=form)
    bots = botstore.all_bots(conn_of(app))
    assert len(bots) == 1 and bots[0]["mode"] == "sim"


def test_nan_or_infinite_capital_is_refused_not_a_crash(env):
    """Achado real de testes de uso (validado pelo Codex): float("nan")/float("inf") passam no parse sem
    levantar ValueError, e chegavam a build_grid() como int(nan)/int(inf) — ValueError/OverflowError não
    apanhados, 500 no painel."""
    c, fm, app = env
    for bad in ("nan", "NaN", "inf", "Infinity", "-inf"):
        r = c.post("/bots/novo", data={"csrf": tok(c, "/bots/novo"), "pair": PAIR, "capital": bad, "step": "create"})
        assert r.status_code in (302, 400), bad          # nunca um 500
    assert botstore.all_bots(conn_of(app)) == []


def test_capital_above_the_sanity_limit_is_refused(env):
    c, fm, app = env
    form = {"csrf": tok(c, "/bots/novo"), "pair": PAIR, "capital": "1e10", "step": "create"}
    html = c.post("/bots/novo", data=form, follow_redirects=True).get_data(as_text=True)
    assert "acima do limite" in html
    assert botstore.all_bots(conn_of(app)) == []


def test_pair_outside_suggestions_or_tiny_capital_is_refused(env):
    c, fm, app = env
    base = {"csrf": tok(c, "/bots/novo"), "capital": "77", "step": "create"}
    c.post("/bots/novo", data={**base, "pair": "HACKUSDT"})
    c.post("/bots/novo", data={**base, "pair": PAIR, "capital": "10"})      # guarda recusa: capital pequeno demais
    assert botstore.all_bots(conn_of(app)) == []
    html = c.get("/bots/novo").get_data(as_text=True)
    assert "Escolhe um dos pares" in html or "grelha segura" in html


# ---------- corredor ----------
def make_bot(app, capital=77.0):
    conn = conn_of(app)
    return botstore.create(conn, PAIR, capital, RULES, {"max_loss_trade_pct": 2.0, "pause_drawdown_pct": 20.0})


def test_runner_starts_bot_processes_candles_and_writes_heartbeat(env):
    c, fm, app = env
    bid = make_bot(app)
    fm.cs = candles(sine(400, amp=0.05, period=720))
    conn = conn_of(app)
    tick(conn, fm, fm.cs[-1][0] + MIN)
    row = botstore.get(conn, bid)
    assert row["status"] == "running" and row["last_ts"] == fm.cs[-1][0]
    eng = botstore.load_engine(conn, bid)
    assert eng.grid and len(eng.orders) > 0
    assert db.get(conn, "runner_heartbeat")
    dup = conn.execute("SELECT slot, side, COUNT(*) n FROM bot_orders GROUP BY bot_id, slot, side HAVING n > 1").fetchall()
    assert dup == []
    assert "Corredor ativo" in c.get("/bots").get_data(as_text=True)


def test_runner_restart_resumes_without_duplicates_and_matches_a_continuous_run(env):
    c, fm, app = env
    bid = make_bot(app)
    cs = candles(sine(1500, amp=0.05, period=720))
    fm.cs = cs
    conn = conn_of(app)
    tick(conn, fm, cs[0][0] + MIN)                           # o bot arranca na primeira vela
    tick(conn, fm, cs[700][0] + MIN)                         # a meio
    conn.close()
    conn = conn_of(app)                                                       # "o Pi reiniciou": nova ligação, mesmo estado
    tick(conn, fm, cs[-1][0] + MIN)
    resumed = botstore.load_engine(conn, bid)
    ref = Engine.new(PAIR, 77.0, RULES, {"max_loss_trade_pct": 2.0, "pause_drawdown_pct": 20.0})
    for candle in cs:                                        # referência: uma corrida contínua, sem reinícios
        ref.process_candle(candle)
    assert resumed.s["cycles"] == ref.s["cycles"]
    assert resumed.equity(cs[-1][4]) == pytest.approx(ref.equity(cs[-1][4]))
    order_key = lambda o: (o["slot"], o["side"])
    assert sorted(resumed.orders, key=order_key) == sorted(ref.orders, key=order_key)   # as mesmas ordens, sem duplicados


def test_panel_commands_and_parar_tudo_reach_the_bot_through_the_runner(env):
    c, fm, app = env
    bid = make_bot(app)
    fm.cs = candles(sine(200, amp=0.02, period=720))
    conn = conn_of(app)
    tick(conn, fm, fm.cs[-1][0] + MIN)
    c.post(f"/bots/{bid}/comando", data={"csrf": tok(c, f"/bots/{bid}"), "cmd": "pause"})
    assert botstore.get(conn_of(app), bid)["status"] == "running"             # o painel só pede; quem age é o corredor
    tick(conn_of(app), fm, fm.cs[-1][0] + MIN)
    assert botstore.get(conn_of(app), bid)["status"] == "paused"
    c.post(f"/bots/{bid}/comando", data={"csrf": tok(c, f"/bots/{bid}"), "cmd": "resume"})
    tick(conn_of(app), fm, fm.cs[-1][0] + MIN)
    assert botstore.get(conn_of(app), bid)["status"] == "running"
    c.post("/parar-tudo", data={"csrf": tok(c, "/parar-tudo")})
    tick(conn_of(app), fm, fm.cs[-1][0] + MIN)
    row = botstore.get(conn_of(app), bid)
    assert row["status"] == "stopped" and "PARAR TUDO" in row["reason"]
    assert conn_of(app).execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id=?", (bid,)).fetchone()[0] == 0


def test_no_prices_means_warning_and_no_invented_data(env):
    c, fm, app = env
    bid = make_bot(app)
    fm.cs = candles(sine(200, amp=0.02, period=720))
    conn = conn_of(app)
    tick(conn, fm, fm.cs[-1][0] + MIN)
    before = botstore.get(conn, bid)["last_ts"]
    fm.fail = "Sem ligação à Binance."
    tick(conn, fm, fm.cs[-1][0] + 30 * MIN)
    row = botstore.get(conn, bid)
    assert row["last_ts"] == before and "Sem dados da Binance" in row["warning"] and row["status"] == "running"
    assert "Sem dados da Binance" in c.get(f"/bots/{bid}").get_data(as_text=True)
    fm.fail = None
    tick(conn, fm, fm.cs[-1][0] + MIN)
    assert botstore.get(conn, bid)["warning"] == ""


def test_stop_loss_via_runner_records_reason_and_closes_everything(env):
    c, fm, app = env
    bid = make_bot(app)
    fm.cs = candles(flat(30) + line(100, 85, 180) + flat(120, 85))
    conn = conn_of(app)
    tick(conn, fm, fm.cs[0][0] + MIN)
    tick(conn, fm, fm.cs[-1][0] + MIN)
    row = botstore.get(conn, bid)
    assert row["status"] == "stopped" and "stop-loss" in row["reason"], (row["status"], row["reason"], row["warning"])
    eng = botstore.load_engine(conn, bid)
    assert eng.s["base"] == 0 and eng.orders == [] and eng.s["worst_loss_pct"] <= 2.0


def test_seven_days_without_intervention_meets_the_v02_criteria(env):
    c, fm, app = env
    bid = make_bot(app)
    days = 7
    fm.cs = candles(sine(days * 1440, amp=0.04, period=720))
    conn = conn_of(app)
    tick(conn, fm, fm.cs[0][0] + MIN)
    step = 1000                                                             # o corredor processa em páginas
    for k in range(step, days * 1440 + step, step):
        tick(conn, fm, fm.cs[min(k, len(fm.cs)) - 1][0] + MIN)
    row = botstore.get(conn, bid)
    eng = botstore.load_engine(conn, bid)
    st = botstore.stats(eng, fm.cs[-1][0] + MIN)
    assert row["status"] == "running", row["reason"]                       # 7 dias seguidos sem intervenção
    assert st["running_hours"] >= 167
    assert st["worst_loss_pct"] <= 2.0                                     # nenhuma operação acima de 2%
    assert st["cycles"] > 5                                              # a grelha trabalha (o número depende do mercado)
    fills = conn.execute("SELECT SUM(fee) f, SUM(pnl) p FROM bot_fills WHERE bot_id=?", (bid,)).fetchone()
    assert fills["f"] == pytest.approx(eng.s["fees"])                       # comissões todas registadas
    assert st["net_profit"] == pytest.approx(eng.equity(fm.cs[-1][4]) - 77.0)   # lucro mostrado = capital final - inicial
    assert st["realized"] == pytest.approx(fills["p"])                       # já líquido de comissões (o pnl de cada ciclo desconta-as)
    html = c.get("/estatisticas").get_data(as_text=True)
    assert "7 dias seguidos sem intervenção" in html and "Comissões pagas" in html and "<polyline" in html


def test_daily_loss_limit_stops_the_bot_by_itself_via_runner(env):
    c, fm, app = env
    bid = make_bot(app)
    conn = conn_of(app)
    conn.execute("UPDATE bots SET params=? WHERE id=?", (json.dumps({"drop_pause_pct": 50, "daily_loss_pct": 1.0,
                                                                     "max_loss_trade_pct": 2.0, "pause_drawdown_pct": 20.0}), bid))
    conn.commit()
    fm.cs = candles(flat(30) + line(100, 95, 60) + flat(30, 95))
    tick(conn, fm, fm.cs[0][0] + MIN)
    tick(conn, fm, fm.cs[-1][0] + MIN)
    row = botstore.get(conn, bid)
    assert row["status"] == "paused" and "limite diário" in row["reason"]


def test_bots_page_warns_when_the_runner_is_not_running(env):
    c, fm, app = env
    assert "nunca correu" in c.get("/bots").get_data(as_text=True)
    for url in ["/bots", "/bots/novo", "/estatisticas"]:
        assert "PARAR TUDO" in c.get(url).get_data(as_text=True), url
    assert "MODO SIMULAÇÃO" in c.get("/bots").get_data(as_text=True)


def test_no_real_order_code_exists_in_the_bot_modules():
    from pathlib import Path
    src = "\n".join(p.read_text(encoding="utf-8") for p in (Path(__file__).resolve().parent.parent / "app").glob("*.py")
                    if p.name != "trader.py")   # v0.3: só trader.py envia ordens, e só para a Testnet
    for forbidden in ["/api/v3/order", "newOrder", 'method="POST"', "def place_order", "def withdraw"]:
        assert forbidden not in src, forbidden
