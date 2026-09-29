"""Critérios de seleção de pares configuráveis, com plano B quando o mercado muda e nenhum par os cumpre."""
import re

import pytest

from app import db, pairs
from app.rules import describe_changes, parse_config
from test_bots import PAIR, ticker


class Form(dict):
    def getlist(self, key):
        v = self.get(key, [])
        return v if isinstance(v, list) else [v]


def market(**by_pair):
    """Mercado falso: cada par com os seus números (vol em USDT, spread por bid/ask, amplitude por high/low)."""
    out = {}
    for sym, kw in by_pair.items():
        out[sym] = {**ticker(**kw), "symbol": sym}
    return out


CALM = market(AUSDT=dict(vol="30000000", high="100.6", low="99.9"),           # 0,7% de movimento: abaixo do mínimo
              BUSDT=dict(vol="8000000", high="103", low="98"),                  # pouco volume
              CUSDT=dict(vol="90000000", high="101", low="99.5"))               # 1,5%: abaixo do mínimo de 2%


# ---------- valores por defeito = comportamento anterior ----------
def test_defaults_reproduce_the_previous_behaviour():
    p = pairs.params_from(pairs.DEFAULTS)
    assert (p["volume"], p["spread"], p["range_min"], p["range_max"], p["range_target"]) == (20e6, 0.05, 2.0, 8.0, 4.0)
    good = market(GOODUSDT=dict())
    assert [x["pair"] for x in pairs.suggest(good)] == ["GOODUSDT"] == [x["pair"] for x in pairs.suggest(good, params=p)]
    assert db.DEFAULTS["pair_min_volume"] == "20" and db.DEFAULTS["pair_adaptive"] == "1"


def test_changing_a_criterion_changes_the_result():
    t = market(GOODUSDT=dict(vol="30000000"))
    assert pairs.suggest(t)                                                   # 30 M cumpre os 20 M
    strict = pairs.params_from({**pairs.DEFAULTS, "pair_min_volume": "50"})
    assert pairs.suggest(t, params=strict) == []                              # com 50 M já não
    tight = pairs.params_from({**pairs.DEFAULTS, "pair_max_spread": "0.001"})
    assert pairs.suggest(t, params=tight) == []
    wide = pairs.params_from({**pairs.DEFAULTS, "pair_range_min": "0.5", "pair_range_max": "2"})
    assert pairs.suggest(market(QUIETUSDT=dict(high="100.6", low="99.9")), params=wide)[0]["pair"] == "QUIETUSDT"


def test_the_target_only_orders_it_does_not_exclude():
    t = market(AUSDT=dict(high="102", low="99.5"), BUSDT=dict(high="105", low="99"))       # 2,5% e 6%
    near_low = pairs.params_from({**pairs.DEFAULTS, "pair_range_target": "2.5"})
    near_high = pairs.params_from({**pairs.DEFAULTS, "pair_range_target": "6"})
    assert [x["pair"] for x in pairs.suggest(t, params=near_low)] == ["AUSDT", "BUSDT"]
    assert [x["pair"] for x in pairs.suggest(t, params=near_high)] == ["BUSDT", "AUSDT"]


def test_bad_stored_values_fall_back_to_the_starting_points():
    assert pairs.params_from({"pair_min_volume": "lixo", "pair_max_spread": None})["volume"] == 20e6


# ---------- plano B quando nada cumpre ----------
def test_when_nothing_meets_the_criteria_they_are_widened_in_steps_and_it_says_so():
    sel = pairs.select(CALM)                                                  # nada cumpre o critério normal
    assert sel["level"] >= 1 and sel["items"] and "Alarguei" in sel["note"]
    assert all(not i["meets"] for i in sel["items"])                          # todos marcados como fora dos critérios
    assert all(i["why"][0].startswith("Fora dos teus critérios") for i in sel["items"])
    assert "spread" in sel["note"] and "M USDT" in sel["note"]


def test_the_first_level_that_finds_something_is_used_and_strict_matches_come_first():
    both = {**market(GOODUSDT=dict()), **CALM}
    sel = pairs.select(both)
    assert sel["level"] == 0 and [i["pair"] for i in sel["items"]] == ["GOODUSDT"] and sel["note"] == ""
    mild = market(AUSDT=dict(vol="12000000"))                                 # falha só o volume: nível 1 resolve
    sel = pairs.select(mild)
    assert sel["level"] == 1 and sel["items"][0]["pair"] == "AUSDT" and "volume" in sel["items"][0]["failed"][0]


def test_when_even_the_widened_criteria_fail_the_closest_pairs_are_shown_marked():
    awful = market(XUSDT=dict(vol="500000", high="130", low="80"), YUSDT=dict(vol="900000", bid="90", ask="110"))
    sel = pairs.select(awful)
    assert sel["level"] == 3 and len(sel["items"]) == 2 and "mais próximos" in sel["note"]
    assert all(not i["meets"] for i in sel["items"])


def test_with_adaptation_off_it_explains_why_the_list_is_empty():
    sel = pairs.select(CALM, adaptive=False)
    assert sel["items"] == [] and "Nenhum par cumpre os critérios atuais" in sel["note"] and "Alarga-os" in sel["note"]


def test_no_market_data_means_no_invented_suggestions():
    sel = pairs.select({})
    assert sel["items"] == [] and sel["note"] == ""


def test_exclusions_still_apply_in_every_step():
    t = market(DOGEUSDT=dict(), USDCUSDT=dict())
    assert pairs.select(t, exclusions=("memecoins", "leveraged", "stables"))["items"] == []
    assert pairs.select(t, exclusions=())["items"]


def test_testnet_restriction_is_applied_before_widening():
    t = {**market(GOODUSDT=dict()), **market(ONLYTESTUSDT=dict(vol="10000000"))}
    sel = pairs.select(t, only={"ONLYTESTUSDT"})
    assert [i["pair"] for i in sel["items"]] == ["ONLYTESTUSDT"] and sel["level"] == 1


# ---------- validação e alterações ----------
def form(**kw):
    base = {"capital_eur": "560", "split_reserve": "60", "split_trading": "30", "split_cash": "10", "max_loss_trade": "2",
            "profit_to_reserve": "50", "pause_drawdown": "20", "min_bot_capital": "20", "pair_min_volume": "20", "pair_max_spread": "0.05",
            "pair_range_min": "2", "pair_range_max": "8", "pair_range_target": "4", "pair_adaptive_sent": "1"}
    base.update(kw)
    return Form(base)


def test_parse_accepts_valid_criteria_and_rejects_bad_ones():
    ok, err = parse_config(form(pair_min_volume="5", pair_adaptive="1"))
    assert not err and ok["pair_min_volume"] == "5" and ok["pair_adaptive"] == "1"
    assert parse_config(form())[0]["pair_adaptive"] == "0"                    # a caixa desmarcada não vai no formulário
    for bad, msg in [({"pair_range_min": "9"}, "máximo tem de ser maior"), ({"pair_range_target": "12"}, "entre o mínimo e o máximo"),
                     ({"pair_min_volume": "0"}, "entre"), ({"pair_max_spread": "abc"}, "número"), ({"pair_min_volume": "99999"}, "entre")]:
        _, errors = parse_config(form(**bad))
        assert errors and any(msg in e for e in errors), (bad, errors)


def test_changes_are_described_with_readable_labels():
    old = {**db.DEFAULTS}
    new, _ = parse_config(form(pair_min_volume="5"))
    labels = dict((a, (b, c)) for a, b, c in describe_changes(old, {**old, **new}))
    assert labels["Pares: volume mínimo em 24 h (M USDT)"] == ("20", "5")
    assert labels["Pares: alargar os critérios se nenhum par cumprir"] == ("sim", "não")


# ---------- painel ----------
@pytest.fixture
def env(tmp_path):
    from test_bots import FakeMarket
    from test_grid import flat
    from test_bots import candles
    from app import create_app, market as market_mod
    market_mod.clear_cache()
    fm = FakeMarket(candles(flat(5)))
    app = create_app({"DATA_DIR": str(tmp_path / "data"), "KEY_FILE": str(tmp_path / "k.json"),
                      "MARKET_FETCH": lambda s: {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "EURUSDT": 1.1},
                      "READER_FACTORY": lambda k, s: fm, "TESTING": True})
    c = app.test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/setup").get_data(as_text=True)).group(1)
    c.post("/setup", data={"csrf": tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    return c, fm, app


def csrf(c, url):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def test_config_page_has_the_criteria_with_recommendations_and_explanations(env):
    c, fm, app = env
    html = c.get("/configuracao?cat=sugestoes").get_data(as_text=True)
    for word in ("Critérios para escolher pares", "Volume mínimo em 24 h", "Spread máximo", "Movimento mínimo", "Movimento máximo",
                 "Movimento ideal", "alargar os critérios", "pontos de partida a validar", "o mercado muda"):
        assert word in html, word
    for key in ("pair_min_volume", "pair_max_spread", "pair_range_min", "pair_range_max", "pair_range_target", "pair_adaptive"):
        assert f'name="{key}"' in html
    assert html.count("Erro comum") >= 12 and "data-apply" in html
    assert "volume acima de 20 M USDT, spread até 0.05%" in html


def test_saving_criteria_goes_through_the_confirmation_and_refreshes_suggestions(env):
    c, fm, app = env
    conn = db.connect(app.config["DB_PATH"])
    assert "GOODUSDT" not in c.get("/bots/novo").get_data(as_text=True)
    fm.tickers = market(GOODUSDT=dict(vol="8000000"))                          # 8 M: abaixo dos 20 M por defeito
    assert "fora dos critérios" in c.get("/bots/novo?atualizar=1").get_data(as_text=True)
    data = {**form(pair_min_volume="5"), "csrf": csrf(c, "/configuracao?cat=sugestoes"), "cat": "sugestoes", "pair_adaptive": "1"}
    data.pop("pair_adaptive_sent")
    r = c.post("/configuracao", data={**data, "exclusions": ["memecoins", "leveraged"]})
    assert "Pares: volume mínimo" in r.get_data(as_text=True)                  # resumo antes → depois
    c.post("/configuracao", data={**data, "exclusions": ["memecoins", "leveraged"], "step": "save"})
    assert db.get(conn, "pair_min_volume") == "5"
    page = c.get("/bots/novo").get_data(as_text=True)                          # a cache foi refeita com o critério novo
    assert "GOODUSDT" in page and "fora dos critérios" not in page and "Alarguei" not in page


def test_new_bot_page_explains_the_widening_and_marks_the_pairs(env):
    c, fm, app = env
    fm.tickers = CALM
    page = c.get("/bots/novo?atualizar=1").get_data(as_text=True)
    assert "Nenhum par cumpria os teus critérios" in page and "Rever critérios" in page
    assert page.count("fora dos critérios") >= 1 and 'class="pill warn"' in page
    assert 'name="pair"' in page                                               # e dá para escolher (com cuidado)


def test_new_bot_page_with_adaptation_off_shows_the_reason_and_a_link_to_the_settings(env):
    c, fm, app = env
    conn = db.connect(app.config["DB_PATH"])
    db.set_many(conn, {"pair_adaptive": "0"})
    fm.tickers = CALM
    page = c.get("/bots/novo?atualizar=1").get_data(as_text=True)
    assert "Nenhum par cumpre os critérios atuais" in page and "cat=sugestoes" in page and 'name="pair"' not in page


from test_testnet import panel  # noqa: E402,F401


def test_testnet_page_applies_the_criteria_only_to_pairs_that_exist_there_and_widens_them(panel):
    c, ex, app, tmp = panel
    fm = app.config["READER_FACTORY"](None, None)
    fm.tickers = {**market(ABCUSDT=dict(vol="8000000")), **market(GOODUSDT=dict())}     # GOOD cumpre, mas não existe na Testnet
    ex.trading_symbols = lambda: {"ABCUSDT"}
    page = c.get("/bots/novo?modo=testnet&atualizar=1").get_data(as_text=True)
    assert "ABCUSDT" in page and "GOODUSDT" not in page                       # só o que existe na Testnet
    assert "fora dos critérios" in page and "Nenhum par cumpria os teus critérios" in page
