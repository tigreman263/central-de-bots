"""v0.3 (docs/V03.md): bot de grelha a enviar ordens na Testnet. Tudo testado contra uma exchange FALSA em memória:
nenhum destes testes fala com a Binance. Critérios cobertos: retoma sem duplicar (5 cortes), ids únicos, paragem geral,
filtros, comissões reais, reset da Testnet e isolamento das chaves."""
import json
import re
from pathlib import Path

import pytest

from app import botstore, db, keystore, notify, runner
from app import grid as G
from app.engine import MIN, PAUSED, RUNNING, STOPPED
from app.trader import Trader, TraderError
from test_grid import RULES, flat
from test_grid import candles as _candles

T0 = 1_699_999_200_000
PAIR = "XYZUSDT"
FEE = 0.001


class Crash(BaseException):
    """Simula o processo a ser morto: não é apanhada pelos 'except Exception' do corredor."""


class FakeExchange:
    """Testnet falsa: livro de ordens, execuções com comissão real, filtros, falhas e reset."""

    def __init__(self, cs):
        self.cs, self.now = cs, None
        self.rules = {**RULES, "min_qty": 0.001, "max_qty": 9e6, "status": "TRADING"}
        self.orders, self.trades, self.placed = {}, {}, {}
        self.next_id, self.rejections = 1, 0
        self.fail, self.ghost_next_place = {}, False
        self.commission_in_base = True                       # como a Binance: compra paga a comissão na moeda comprada
        self.commission_asset = None                         # "BNB": comissão noutra moeda (não mexe nestes saldos)
        self.has_keys = True
        self.bal = {"USDT": 1_000_000.0, "XYZ": 0.0}
        self.crash_after_places = None                       # o processo "morre" depois de N ordens enviadas

    # ---- dados públicos ----
    def klines_1m(self, symbol, start_ms=None, limit=1000):
        closed = [c for c in self.cs if self.now is None or c[0] + MIN <= self.now]
        rows = [c for c in closed if start_ms is None or c[0] >= start_ms]
        rows = rows[-limit:] if start_ms is None else rows[:limit]
        return [[c[0], str(c[1]), str(c[2]), str(c[3]), str(c[4])] for c in rows]

    def price(self):
        return [c for c in self.cs if self.now is None or c[0] + MIN <= self.now][-1][4]

    def symbol_rules(self, symbol):
        return dict(self.rules)

    # ---- ajudas dos testes ----
    def _maybe_fail(self, what):
        exc = self.fail.pop(what, None)
        if exc:
            raise exc

    def _record_trade(self, o, qty, price):
        quote = qty * price
        if o["side"] == "BUY":
            fee, asset = (qty * FEE, "XYZ") if self.commission_in_base else (quote * FEE, "USDT")
        else:
            fee, asset = quote * FEE, "USDT"
        if self.commission_asset:
            fee, asset = quote * FEE, self.commission_asset
        # saldos reais: a compra soma moeda (menos a comissão em moeda), a venda soma USDT (menos a comissão em USDT)
        if o["side"] == "BUY":
            self.bal["USDT"] -= quote + (fee if asset == "USDT" else 0.0)
            self.bal["XYZ"] += qty - (fee if asset == "XYZ" else 0.0)
        else:
            self.bal["XYZ"] -= qty
            self.bal["USDT"] += quote - (fee if asset == "USDT" else 0.0)
        self.trades.setdefault(o["orderId"], []).append(
            {"price": str(price), "qty": str(qty), "quoteQty": str(quote), "commission": str(fee),
             "commissionAsset": asset})
        o["executedQty"] = str(float(o["executedQty"]) + qty)
        o["cummulativeQuoteQty"] = str(float(o["cummulativeQuoteQty"]) + quote)

    def fill(self, cid, qty=None):
        """A Testnet executa (toda ou parte de) uma ordem nossa ao preço dela."""
        o = self.orders[cid]
        remaining = float(o["origQty"]) - float(o["executedQty"])
        q = remaining if qty is None else qty
        self._record_trade(o, q, float(o["price"]))
        o["status"] = "FILLED" if abs(float(o["origQty"]) - float(o["executedQty"])) < 1e-12 else "PARTIALLY_FILLED"

    def reset(self):
        self.orders, self.trades = {}, {}
        self.bal = {"USDT": 1_000_000.0, "XYZ": 0.0}

    def open_cids(self):
        return {c for c, o in self.orders.items() if o["status"] in ("NEW", "PARTIALLY_FILLED")}

    # ---- conta e ordens ----
    def _view(self, o):
        return {k: v for k, v in o.items() if k != "fills"}

    def open_orders(self, symbol):
        self._maybe_fail("open_orders")
        return [self._view(o) for o in self.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED")]

    def get_order(self, symbol, cid):
        self._maybe_fail("get_order")
        if cid not in self.orders:
            raise TraderError("A ordem não existe na Testnet.", -2013)
        return self._view(self.orders[cid])

    def order_trades(self, symbol, order_id):
        return self.trades.get(order_id, [])

    def _locked_sells(self):
        return sum(float(o["origQty"]) - float(o["executedQty"]) for o in self.orders.values()
                   if o["side"] == "SELL" and o["status"] in ("NEW", "PARTIALLY_FILLED"))

    def account(self):
        return [{"asset": "USDT", "free": str(self.bal["USDT"]), "locked": "0"},
                {"asset": "XYZ", "free": str(max(0.0, self.bal["XYZ"] - self._locked_sells())),
                 "locked": str(self._locked_sells())}]

    def free_balance(self, asset):
        return float(next(b["free"] for b in self.account() if b["asset"] == asset))

    def _new(self, side, kind, qty, price, cid):
        if cid in self.orders:
            raise TraderError("duplicada", -2010)
        if side == "sell" and self.bal["XYZ"] - self._locked_sells() < qty - 1e-12:
            raise TraderError("saldo insuficiente", -2010)
        r = self.rules
        if abs(qty / r["step"] - round(qty / r["step"])) > 1e-6 or qty < r["min_qty"] or \
                (kind == "limit" and abs(price / r["tick"] - round(price / r["tick"])) > 1e-6) or \
                qty * price < r["min_notional"]:
            self.rejections += 1
            raise TraderError("filtro", -1013)
        o = {"orderId": self.next_id, "clientOrderId": cid, "side": side.upper(), "type": kind.upper(),
             "origQty": str(qty), "price": str(price), "executedQty": "0", "cummulativeQuoteQty": "0", "status": "NEW"}
        self.next_id += 1
        self.orders[cid] = o
        self.placed[cid] = self.placed.get(cid, 0) + 1
        return o

    def place_limit(self, symbol, side, qty, price, cid, tick=None, step=None):
        self._maybe_fail("place")
        o = self._new(side, "limit", qty, price, cid)
        if self.crash_after_places is not None:
            self.crash_after_places -= 1
            if self.crash_after_places <= 0:
                self.crash_after_places = None
                raise Crash()                                   # a ordem chegou e o processo morreu
        if self.ghost_next_place:                              # a ordem chegou, mas a resposta perdeu-se
            self.ghost_next_place = False
            raise TraderError("Sem ligação à Testnet.")
        return {"orderId": o["orderId"], "clientOrderId": cid}

    def place_market(self, symbol, side, qty, cid, step=None):
        self._maybe_fail("place")
        px = self.price()
        o = self._new(side, "market", qty, px, cid)
        self._record_trade(o, qty, px)
        o["status"] = "FILLED"
        return {**self._view(o), "fills": list(self.trades[o["orderId"]])}

    def cancel_order(self, symbol, cid):
        self._maybe_fail("cancel")
        o = self.orders.get(cid)
        if o is None or o["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise TraderError("A ordem a cancelar não existe na Testnet.", -2011)
        o["status"] = "CANCELED"
        return self._view(o)


# ---------- ambiente ----------
def candles(path):
    return _candles(path, start=T0)


@pytest.fixture
def world(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    ex = FakeExchange(candles(flat(30)))
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    return conn, ex, bid


def tick(conn, ex, bid, k):
    """Um passo do corredor, k minutos depois do início."""
    ex.now = T0 + k * MIN
    runner.tick_bot(conn, ex, bid, ex.now, trader=ex)


def engine(conn, bid):
    return botstore.load_engine(conn, bid)


def assert_consistent(conn, ex, bid):
    """A exchange e o estado local coincidem 1 para 1 e nunca houve um id enviado duas vezes."""
    eng = engine(conn, bid)
    local = [o for o in eng.orders if o.get("cid")]
    assert all(o["state"] in ("open", "partial") for o in local), [o["state"] for o in local]
    assert {o["cid"] for o in local} == ex.open_cids()
    assert len({o["cid"] for o in local}) == len(local)
    assert all(n == 1 for n in ex.placed.values()), {c: n for c, n in ex.placed.items() if n != 1}
    sells = sum(o["qty"] for o in local if o["side"] == "sell")
    assert sells <= eng.s["base"] + 1e-9                     # nunca há mais vendas do que moeda em carteira


def first_order(ex, side):
    return next(c for c, o in ex.orders.items() if o["side"] == side.upper() and o["status"] == "NEW"
                and o["type"] == "LIMIT")


# ---------- montagem, ids, filtros ----------
def test_bot_places_real_orders_with_unique_ids_and_valid_filters(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    eng = engine(conn, bid)
    assert eng.status == RUNNING and eng.mode == "testnet"
    assert any(o["type"] == "MARKET" for o in ex.orders.values())          # compra inicial ao mercado
    sells = [o for o in ex.orders.values() if o["side"] == "SELL"]
    buys = [o for o in ex.orders.values() if o["side"] == "BUY" and o["type"] == "LIMIT"]
    assert sells and buys
    assert ex.rejections == 0                                               # zero erros -1013
    assert all(o["clientOrderId"].startswith(f"cb{bid}") and len(o["clientOrderId"]) <= 36 for o in ex.orders.values())
    assert_consistent(conn, ex, bid)
    tick(conn, ex, bid, 2)                                                  # passo seguinte não repete nada
    assert_consistent(conn, ex, bid)


def test_guard_refuses_bad_orders_before_they_leave():
    good = {"side": "buy", "type": "limit", "qty": 0.1, "price": 100.0}
    rules = {**RULES, "min_qty": 0.001, "max_qty": 1000.0}
    G.validate_order(good, rules, "testnet")
    for bad, why in [({**good, "qty": 0.1005}, "passo"), ({**good, "price": 100.005}, "preço"),
                     ({**good, "qty": 0.01}, "mínimo"), ({**good, "qty": 2000.0}, "limites"),
                     ({**good, "side": "hold"}, "tipo")]:
        with pytest.raises(G.OrderRefused):
            G.validate_order(bad, rules, "testnet")
    with pytest.raises(G.OrderRefused):
        G.validate_order(good, rules, "sim")                                # o simulador nunca envia ordens
    with pytest.raises(G.OrderRefused):
        G.validate_order(good, rules, "testnet", open_count=G.MAX_OPEN_ORDERS)


# ---------- contabilidade com valores reais ----------
def test_cycle_uses_real_prices_and_real_commissions(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    ex.fill(buy)                                                            # compra executa (comissão em moeda)
    tick(conn, ex, bid, 2)
    eng = engine(conn, bid)
    assert_consistent(conn, ex, bid)
    slot = next(o for o in eng.orders if o["side"] == "sell" and o["price"] > 0)
    assert any(o["side"] == "sell" for o in eng.orders)
    sell = next(c for c, o in ex.orders.items() if o["side"] == "SELL" and o["status"] == "NEW"
                and float(o["price"]) > float(ex.orders[buy]["price"]))
    ex.fill(sell)
    tick(conn, ex, bid, 3)
    eng = engine(conn, bid)
    assert eng.s["cycles"] >= 1
    real_fees = 0.0
    for oid, trs in ex.trades.items():
        for t in trs:
            real_fees += float(t["commission"]) * (float(t["price"]) if t["commissionAsset"] == "XYZ" else 1.0)
    assert eng.s["fees"] == pytest.approx(real_fees, rel=1e-6)              # comissões reais, não a taxa fixa
    assert_consistent(conn, ex, bid)


def test_fee_paid_in_coin_never_leaves_more_sells_than_coins(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    for _ in range(2):
        ex.fill(first_order(ex, "buy"))
    tick(conn, ex, bid, 2)
    assert_consistent(conn, ex, bid)                                        # inclui vendas <= moeda em carteira


def test_partial_fill_then_cancel_keeps_only_what_was_executed(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    ex.fill(buy, qty=float(ex.orders[buy]["origQty"]) / 2)
    tick(conn, ex, bid, 2)
    assert engine(conn, bid).orders and any(o.get("state") == "partial" for o in engine(conn, bid).orders)
    botstore.set_command(conn, bid, "pause")                                # pausa: as compras são canceladas
    tick(conn, ex, bid, 3)
    eng = engine(conn, bid)
    assert eng.status == PAUSED
    assert ex.orders[buy]["status"] == "CANCELED"
    assert eng.s["base"] > 0                                                # ficou com a parte executada
    assert_consistent(conn, ex, bid)


# ---------- retoma sem duplicar: 5 cortes ----------
def _crash_flush(monkeypatch):
    def boom(*a, **k):
        raise Crash()
    monkeypatch.setattr(runner, "_flush", boom)


def test_crash_1_after_saving_orders_before_sending_them(world, monkeypatch):
    conn, ex, bid = world
    with monkeypatch.context() as m:
        _crash_flush(m)
        with pytest.raises(Crash):
            tick(conn, ex, bid, 1)
    assert not ex.orders                                                    # nada saiu
    assert all(o["state"] == "sending" for o in engine(conn, bid).orders if o.get("cid"))
    tick(conn, ex, bid, 2)
    assert_consistent(conn, ex, bid)
    assert ex.rejections == 0


def test_crash_2_order_reached_the_exchange_but_the_answer_was_lost(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    ex.fill(first_order(ex, "buy"))                                         # liberta um degrau: haverá uma ordem nova
    ex.ghost_next_place = True
    tick(conn, ex, bid, 2)                                                  # o envio "perdeu" a resposta
    assert any(o.get("state") == "sending" for o in engine(conn, bid).orders)
    tick(conn, ex, bid, 3)                                                  # o arranque seguinte encontra-a pelo id
    assert not ex.ghost_next_place
    assert_consistent(conn, ex, bid)                                        # inclui: nenhum id enviado duas vezes


def test_crash_3_after_partial_fill_before_saving_counts_it_once(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    slot = int(buy.split("-")[1][:-1])
    base_before = engine(conn, bid).s["base"]
    half = float(ex.orders[buy]["origQty"]) / 2
    ex.fill(buy, qty=half)
    ex.now = T0 + 2 * MIN
    eng = engine(conn, bid)
    runner.TestnetExecutor(ex).reconcile(eng, ex.now)                       # aplicado em memória... e o processo morre
    tick(conn, ex, bid, 2)
    tick(conn, ex, bid, 3)
    assert_consistent(conn, ex, bid)
    eng = engine(conn, bid)
    resting = next(o for o in eng.orders if o["cid"] == buy)
    assert resting["state"] == "partial" and resting["exec_qty"] == pytest.approx(half)
    assert eng.s["base"] == pytest.approx(base_before)                      # a parte executada só conta quando a ordem fecha
    assert not [f for f in botstore.fills(conn, bid, limit=1000) if f["side"] == "buy" and f["slot"] == slot]


def test_crash_4_after_full_fill_before_saving_counts_it_once(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    buy = first_order(ex, "buy")
    ex.fill(buy)
    ex.now = T0 + 2 * MIN
    eng = engine(conn, bid)
    runner.TestnetExecutor(ex).reconcile(eng, ex.now)                       # não gravado: o processo morre aqui
    before = len(botstore.fills(conn, bid, limit=1000))
    tick(conn, ex, bid, 2)
    tick(conn, ex, bid, 3)
    tick(conn, ex, bid, 4)
    after = botstore.fills(conn, bid, limit=1000)
    assert len(after) == before + 1                                         # exatamente uma execução nova
    assert_consistent(conn, ex, bid)


def test_crash_5_cancel_done_on_the_exchange_but_not_recorded(world, monkeypatch):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    botstore.set_command(conn, bid, "pause")
    with monkeypatch.context() as m:
        _crash_flush(m)
        with pytest.raises(Crash):
            tick(conn, ex, bid, 2)
    assert engine(conn, bid).s["cancel_queue"]                              # a fila de cancelamentos sobreviveu
    eng = engine(conn, bid)
    runner.TestnetExecutor(ex)._cancel_one(eng, eng.s["cancel_queue"][0], ex.now)   # cancelou mas morreu antes de gravar
    tick(conn, ex, bid, 3)
    eng = engine(conn, bid)
    assert eng.status == PAUSED and not eng.s["cancel_queue"]
    assert_consistent(conn, ex, bid)


# ---------- paragem geral ----------
def test_emergency_stop_cancels_everything_and_closes_the_position_in_one_sweep(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    assert ex.open_cids() and engine(conn, bid).s["base"] > 0
    db.set_many(conn, {"emergency_stop": "1"})
    ex.now = T0 + 1 * MIN + 5_000                                           # 5 s depois: o vigilante, não o passo de 60 s
    runner.emergency_sweep(conn, ex, ex.now)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and eng.reason == "PARAR TUDO"
    assert not ex.open_cids()                                               # todas as ordens canceladas
    assert eng.s["base"] < 0.0015 and not eng.orders                        # posição fechada (só sobra pó abaixo do passo)
    assert any(o["side"] == "SELL" and o["type"] == "MARKET" for o in ex.orders.values())
    assert botstore.stats(eng, ex.now)["net_pct"] > -3                      # a liquidação foi ao preço real, sem deslize inventado


def test_emergency_stop_retries_when_the_network_fails_and_alerts(world, isolated_notifications, tmp_path):
    conn, ex, bid = world
    keystore.save(tmp_path / "tg.json", "123:token", "999")
    notify.TELEGRAM_FILE = tmp_path / "tg.json"
    tick(conn, ex, bid, 1)
    db.set_many(conn, {"emergency_stop": "1"})
    ex.fail["cancel"] = TraderError("Sem ligação à Testnet.")
    ex.now = T0 + 1 * MIN + 5_000
    runner.emergency_sweep(conn, ex, ex.now)
    assert engine(conn, bid).s["cancel_queue"]                              # falhou: fica na fila, não se perde
    runner.emergency_sweep(conn, ex, ex.now + 5_000)                        # 5 s depois tenta outra vez
    eng = engine(conn, bid)
    assert not ex.open_cids() and not eng.s["cancel_queue"] and eng.status == STOPPED
    assert any("PARAR TUDO" in m or "parado" in m.lower() for m in isolated_notifications)   # chegou ao Telegram


# ---------- reset da Testnet ----------
def test_testnet_reset_stops_the_bot_and_alerts_without_counting_a_loss(world, isolated_notifications, tmp_path):
    conn, ex, bid = world
    keystore.save(tmp_path / "tg.json", "123:token", "999")
    notify.TELEGRAM_FILE = tmp_path / "tg.json"
    tick(conn, ex, bid, 1)
    before = engine(conn, bid)
    ex.reset()                                                              # a Testnet foi reposta
    tick(conn, ex, bid, 2)
    eng = engine(conn, bid)
    assert eng.status == STOPPED and "reset da Testnet" in eng.reason and eng.s["testnet_reset"]
    assert not eng.orders and eng.s["realized"] == before.s["realized"]     # não vira perda, nem duplicados
    assert eng.s["worst_loss_pct"] == before.s["worst_loss_pct"]
    assert [e for e in botstore.events(conn, bid) if e["kind"] == "reset"]
    row = conn.execute("SELECT * FROM alerts WHERE key LIKE 'bot:%' AND severity = 'alto'").fetchone()
    assert row and "reset" in row["message"].lower()
    assert any("reset" in m.lower() for m in isolated_notifications)
    tick(conn, ex, bid, 3)                                                  # parado é parado: nada volta a arrancar
    assert engine(conn, bid).status == STOPPED and not ex.orders


# ---------- erros da exchange ----------
def test_filter_refusal_from_the_exchange_pauses_the_bot_safely(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    ex.fill(first_order(ex, "buy"))
    ex.fail["place"] = TraderError("filtro", -1013)
    tick(conn, ex, bid, 2)
    eng = engine(conn, bid)
    assert eng.status == PAUSED and "ordem recusada" in eng.reason


def test_bad_keys_pause_the_bot_and_no_network_just_waits(world):
    conn, ex, bid = world
    tick(conn, ex, bid, 1)
    ex.fail["open_orders"] = TraderError("Sem ligação à Testnet.")
    tick(conn, ex, bid, 2)
    row = botstore.get(conn, bid)
    assert row["status"] == RUNNING and "Sem resposta da Testnet" in row["warning"]
    ex.fail["open_orders"] = TraderError("chave recusada", -2015)
    tick(conn, ex, bid, 3)
    assert botstore.get(conn, bid)["status"] == PAUSED


def test_missing_testnet_keys_makes_the_bot_wait_without_touching_anything(world):
    conn, ex, bid = world
    ex.now = T0 + 60_000
    runner.tick_bot(conn, ex, bid, ex.now, trader=None)
    row = botstore.get(conn, bid)
    assert "chaves da Testnet" in row["warning"] and row["status"] == "pending" and not ex.orders


# ---------- gémeo em simulação com velas da Testnet ----------
def test_sim_twin_reads_testnet_candles_and_sends_no_orders(world):
    conn, ex, bid = world
    twin = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="sim", source="testnet", twin_of=bid)
    tick(conn, ex, bid, 1)
    ex.now = T0 + 1 * MIN
    placed_before = dict(ex.placed)
    runner.tick_bot(conn, object(), twin, ex.now, trader=ex)                # o leitor real nem é usado
    t = engine(conn, twin)
    assert t.mode == "sim" and t.status == RUNNING and t.orders
    assert ex.placed == placed_before                                       # o gémeo nunca envia ordens
    assert botstore.get(conn, twin)["twin_of"] == bid


# ---------- isolamento ----------
def test_trader_only_talks_to_the_testnet_and_never_reads_key_files():
    with pytest.raises(TraderError):
        Trader("k", "s", base="https://api.binance.com")
    src = (Path(__file__).resolve().parent.parent / "app" / "trader.py").read_text(encoding="utf-8")
    for forbidden in ["keystore", "binance_readonly", "Path.home", "read_text"]:
        assert forbidden not in src, forbidden
    assert not re.search(r"(?<![_\w])open\(", src)                            # nenhum ficheiro é aberto aqui
    assert Trader("k", "s").has_keys and not Trader().has_keys
    with pytest.raises(TraderError):
        Trader()._call("GET", "/api/v3/account", signed=True)               # sem chaves nem tenta


def test_only_trader_can_send_or_cancel_orders_and_the_engine_never_touches_the_network():
    app_dir = Path(__file__).resolve().parent.parent / "app"
    for p in app_dir.glob("*.py"):
        if p.name == "trader.py":
            continue
        text = p.read_text(encoding="utf-8")
        assert '"DELETE"' not in text and "/api/v3/order" not in text and "testnet.binance.vision" not in text, p.name
    engine_src = (app_dir / "engine.py").read_text(encoding="utf-8")
    for forbidden in ["urllib", "import requests", "trader", "executor"]:
        assert forbidden not in engine_src.replace("# ", ""), forbidden
    assert "dump" in engine_src   # (engine só decide e emite intenções; o executor concretiza-as)


def test_real_key_file_is_never_used_by_the_order_code(tmp_path, monkeypatch):
    real = tmp_path / "binance_readonly.json"
    keystore.save(real, "REALKEY", "REALSECRET")
    seen = []
    ex = Trader("TESTKEY", "TESTSECRET", opener=lambda req, timeout=8: seen.append(req.headers) or (_ for _ in ()).throw(OSError()))
    with pytest.raises(TraderError):
        ex.place_limit(PAIR, "buy", 0.1, 100.0, "cb1-0b-1", tick=0.01, step=0.001)
    assert seen and seen[0]["X-mbx-apikey"] == "TESTKEY"
    assert "REALKEY" not in json.dumps(seen, default=str)
    assert keystore.testnet_path() != keystore.default_path()


# ---------- painel ----------
@pytest.fixture
def panel(tmp_path):
    from app import create_app, market
    from test_bots import FakeMarket
    market.clear_cache()
    fm = FakeMarket(_candles(flat(5), start=T0))
    ex = FakeExchange(candles(flat(5)))
    ex.trading_symbols = lambda: {"ABCUSDT"}
    ex.ticker24 = lambda s: {"lastPrice": "100"}
    app = create_app({"DATA_DIR": str(tmp_path / "data"), "KEY_FILE": str(tmp_path / "k.json"),
                      "MARKET_FETCH": lambda s: {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "EURUSDT": 1.1},
                      "READER_FACTORY": lambda k, s: fm, "TRADER_FACTORY": lambda k, s: ex,
                      "TESTNET_KEY_FILE": str(tmp_path / "tn.json"), "TELEGRAM_FILE": str(tmp_path / "tg.json"),
                      "WHATSAPP_FILE": str(tmp_path / "wa.json"),
                      "TESTING": True})
    c = app.test_client()
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/setup").get_data(as_text=True)).group(1)
    c.post("/setup", data={"csrf": tok, "password": "uma-palavra-passe-boa", "password2": "uma-palavra-passe-boa"})
    return c, ex, app, tmp_path


def csrf(c, url="/bots/ligacoes"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(url).get_data(as_text=True)).group(1)


def test_panel_saves_testnet_keys_only_after_a_working_test(panel):
    c, ex, app, tmp = panel
    ex.account = lambda: (_ for _ in ()).throw(TraderError("A Testnet recusou a chave.", -2015))
    r = c.post("/bots/ligacoes", data={"csrf": csrf(c), "action": "save_testnet", "api_key": "TESTKEY1234",
                                       "api_secret": "S3CRET"}, follow_redirects=True)
    assert "recusou a chave" in r.get_data(as_text=True) and not (tmp / "tn.json").exists()
    ex.account = lambda: [{"asset": "USDT", "free": "1000", "locked": "0"}]
    r = c.post("/bots/ligacoes", data={"csrf": csrf(c), "action": "save_testnet", "api_key": "TESTKEY1234",
                                       "api_secret": "S3CRET"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert keystore.load(tmp / "tn.json") == ("TESTKEY1234", "S3CRET")
    assert "…1234" in html and "S3CRET" not in html and "TESTKEY1234" not in html     # nunca por inteiro


def test_panel_telegram_is_send_only_and_tested_before_saving(panel, isolated_notifications):
    c, ex, app, tmp = panel
    r = c.post("/configuracao/alertas", data={"csrf": csrf(c, "/configuracao?cat=alertas"), "action": "save_telegram",
                                              "token": "111:AAA", "chat_id": "42"}, follow_redirects=True)
    assert keystore.load(tmp / "tg.json") == ("111:AAA", "42")
    assert any("mensagem de teste" in m for m in isolated_notifications)
    assert "111:AAA" not in r.get_data(as_text=True) and "não recebe comandos" in r.get_data(as_text=True)


def test_panel_creates_a_testnet_bot_with_a_simulation_twin(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    page = c.get("/bots/novo?modo=testnet").get_data(as_text=True)
    assert "ABCUSDT" in page and "gémeo" in page and "Testnet" in page
    data = {"csrf": csrf(c, "/bots/novo?modo=testnet"), "pair": "ABCUSDT", "capital": "77", "modo": "testnet",
            "twin": "1", "step": "preview"}
    # sem chaves: recusa
    ex.has_keys = False
    r = c.post("/bots/novo?modo=testnet", data=data, follow_redirects=True)
    assert "Faltam as chaves da Testnet" in r.get_data(as_text=True) and not botstore.all_bots(conn)
    ex.has_keys = True
    r = c.post("/bots/novo?modo=testnet", data=data)
    assert "Aprovar e criar bot (Testnet)" in r.get_data(as_text=True)
    r = c.post("/bots/novo?modo=testnet", data={**data, "step": "create"}, follow_redirects=True)
    rows = botstore.all_bots(conn)
    assert len(rows) == 2
    real, twin = next(x for x in rows if x["mode"] == "testnet"), next(x for x in rows if x["mode"] == "sim")
    assert real["source"] == "testnet" and twin["source"] == "testnet" and twin["twin_of"] == real["id"]
    html = c.get("/bots").get_data(as_text=True)                                   # abre no separador Testnet
    assert 'aria-current="true">Testnet' in html and "TESTNET" in html and "gémeo do bot" not in html
    sim = c.get("/bots?aba=simulacao").get_data(as_text=True)                      # o gémeo vive no separador Simulação
    assert "SIMULAÇÃO" in sim and "gémeo do bot" in sim
    assert "Simulação vs Testnet" in c.get("/estatisticas").get_data(as_text=True)


def test_testnet_pair_list_only_offers_pairs_that_exist_on_the_testnet(panel):
    c, ex, app, tmp = panel
    ex.trading_symbols = lambda: {"BTCUSDT"}                                # o par sugerido não existe na Testnet
    page = c.get("/bots/novo?modo=testnet&atualizar=1").get_data(as_text=True)
    assert "BTCUSDT" in page and "ABCUSDT" not in page and "plano B" in page
    ex.trading_symbols = lambda: set()
    page = c.get("/bots/novo?modo=testnet&atualizar=1").get_data(as_text=True)
    assert "ABCUSDT" not in page and "BTCUSDT" not in page                  # nada disponível: não inventa pares


# ---------- 7 dias seguidos sem intervenção (mercado sintético + Testnet falsa que executa ao passar o preço) ----------
def match_window(ex, lo_ts, hi_ts):
    window = [c for c in ex.cs if lo_ts <= c[0] and c[0] + MIN <= hi_ts]
    if not window:
        return
    low, high = min(c[3] for c in window), max(c[2] for c in window)
    for cid, o in list(ex.orders.items()):
        if o["status"] not in ("NEW", "PARTIALLY_FILLED") or o["type"] != "LIMIT":
            continue
        px = float(o["price"])
        if (o["side"] == "BUY" and low <= px * (1 - 0.0005)) or (o["side"] == "SELL" and high >= px * (1 + 0.0005)):
            ex.fill(cid)


def test_seven_days_on_the_testnet_without_intervention(tmp_path):
    from test_grid import sine
    days, every = 7, 5
    total = days * 24 * 60
    conn = db.connect(str(tmp_path / "t.db"))
    conn.executescript(db.SCHEMA)
    botstore.init(conn)
    ex = FakeExchange(candles(sine(total + 10, amp=0.04, period=720)))
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    prev = T0
    for k in range(1, total // every):
        now = T0 + k * every * MIN
        match_window(ex, prev, now)
        prev = now
        ex.now = now
        runner.tick_bot(conn, ex, bid, now, trader=ex)
        if k % 200 == 0:
            assert_consistent(conn, ex, bid)
    eng = engine(conn, bid)
    assert_consistent(conn, ex, bid)
    assert eng.status == RUNNING and ex.rejections == 0                     # zero -1013, sem intervenção
    assert eng.s["cycles"] >= 10 and eng.s["worst_loss_pct"] <= 2.0 + 1e-9   # perda por operação dentro do limite
    assert eng.s["day_start_equity"] > 0 and all(v == 1 for v in ex.placed.values())
    real_fees = sum(float(t["commission"]) * (float(t["price"]) if t["commissionAsset"] == "XYZ" else 1.0)
                    for trs in ex.trades.values() for t in trs)
    assert eng.s["fees"] == pytest.approx(real_fees, rel=1e-6)              # contabilidade = execuções reais
    assert botstore.stats(eng, now)["net_pct"] > -5


# ---------- gráfico do bot ----------
def test_bot_page_draws_the_price_chart_with_entries_exits_levels_and_events(panel):
    c, ex, app, tmp = panel
    conn = db.connect(app.config["DB_PATH"])
    bid = botstore.create(conn, PAIR, 77.0, {**ex.rules}, {}, mode="testnet")
    ex.cs = candles([100 + (k % 7) * 0.3 for k in range(60)])
    for k in range(1, 6):
        tick(conn, ex, bid, k)
    ex.fill(first_order(ex, "buy"))
    tick(conn, ex, bid, 6)
    ex.fill(first_order(ex, "sell"))
    tick(conn, ex, bid, 7)
    html = c.get(f"/bots/{bid}").get_data(as_text=True)
    assert 'class="pchart"' in html and 'class="ch-price"' in html                   # linha de preço
    assert html.count('class="ch-buy"') >= 2 and html.count('class="ch-sell"') >= 2   # legenda + marcadores
    assert "ch-lvl buy" in html and "ch-lvl sell" in html and 'class="ch-stop"' in html   # níveis e stop
    assert "Stop " in html and "montagem" in html                                       # eventos e rótulos
    assert 'aria-describedby="pc-d"' in html and "compras e" in html                    # resumo para leitores de ecrã
    assert "TESTNET" in html
    for w in ("6h", "24h", "7d"):
        assert f"janela={w}" in c.get(f"/bots/{bid}?janela={w}").get_data(as_text=True)
    assert c.get(f"/bots/{bid}?janela=lixo").status_code == 200                         # janela inválida: cai para 24h


def test_candles_are_stored_per_bot_and_pruned_after_eight_days(world):
    conn, ex, bid = world
    for k in range(1, 4):
        tick(conn, ex, bid, k)
    rows = botstore.candles(conn, bid)
    assert len(rows) >= 3 and rows == sorted(rows) and len({r[0] for r in rows}) == len(rows)
    conn.execute("INSERT INTO bot_candles VALUES (?, ?, 1, 1, 1, 1)", (bid, T0 - 9 * 24 * 3_600_000))
    conn.commit()
    tick(conn, ex, bid, 4)
    assert all(r[0] >= T0 - 8 * 24 * 3_600_000 for r in botstore.candles(conn, bid))
