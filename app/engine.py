"""Motor do bot de grelha. Nunca fala com a Binance: recebe velas (e, no modo testnet, execuções) e decide.

Estado inteiro serializável: reiniciar o processo retoma exatamente onde ficou, sem duplicar ordens.
Vela: (abertura_ms, open, high, low, close).

Modo "sim": o simulador decide quando as ordens executam. Modo "testnet": as ordens saem para a Testnet por uma camada
à parte e as execuções reais chegam por `on_exchange_fill`; este ficheiro só decide e emite intenções.
"""
import secrets
from datetime import datetime, timezone

from . import grid as G

MIN = 60_000
RUNNING, PAUSED, STOPPED, PENDING = "running", "paused", "stopped", "pending"


def _date(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class Engine:
    def __init__(self, bot):
        self.id = bot.get("id")
        self.pair = bot["pair"]
        self.status = bot["status"]
        self.reason = bot.get("reason", "")
        self.capital = float(bot["capital_usdt"])
        self.p = {**G.DEFAULTS, **bot.get("params", {})}
        self.rules = bot["rules"]
        self.grid = bot.get("grid")
        self.s = bot.get("state") or {}
        self.orders = bot.get("orders", [])
        self.last_ts = bot.get("last_ts", 0)
        self.mode = bot.get("mode", "sim")            # "sim" (simulador) ou "testnet" (ordens na Testnet)
        self.new_fills, self.new_events, self.new_equity, self.new_candles = [], [], [], []

    @classmethod
    def new(cls, pair, capital, rules, params=None, mode="sim"):
        return cls({"pair": pair, "status": PENDING, "capital_usdt": capital, "rules": rules,
                    "params": params or {}, "state": {}, "orders": [], "mode": mode})

    def dump(self):
        return {"mode": self.mode, "pair": self.pair, "status": self.status, "reason": self.reason,
                "capital_usdt": self.capital, "params": self.p, "rules": self.rules, "grid": self.grid,
                "state": self.s, "orders": self.orders, "last_ts": self.last_ts}

    # ---------- utilidades ----------
    def _event(self, ts, kind, detail):
        self.new_events.append({"ts": ts, "kind": kind, "detail": detail})

    def equity(self, close):
        return self.s["quote"] + self.s["reserve"] + self.s["base"] * close

    def _open(self, slot, side):
        return next((o for o in self.orders if o["slot"] == slot and o["side"] == side), None)

    def _cancel_buys(self):
        self._set_orders([o for o in self.orders if o["side"] != "buy"])

    @staticmethod
    def _held(sl):
        """Moeda realmente detida neste degrau (na Testnet é menor que a planeada quando a comissão é paga em moeda)."""
        return sl.get("hold_qty", sl["qty"])

    def _is_flat(self, close):
        """Sem posição: nenhum degrau com moeda e, no máximo, pó abaixo da ordem mínima."""
        if any(sl["holding"] for sl in self.grid["slots"]):
            return False
        return self.s["base"] * close < self.rules["min_notional"] or self.s["base"] <= 1e-12

    # ---------- ordens na exchange (modo testnet) ----------
    def cid_prefix(self):
        """Prefixo dos ids das ordens deste bot. Inclui um sufixo aleatório: recriar a base de dados nunca colide."""
        uid = self.s.get("uid") or self.s.setdefault("uid", secrets.token_hex(3))
        return f"cb{self.id or 0}{uid}-"

    def _set_orders(self, new):
        """Substitui a lista de ordens. As que já estão (ou podem estar) na exchange entram na fila de cancelamento."""
        keep = {id(o) for o in new}
        for o in self.orders:
            if id(o) not in keep and o.get("cid") and o.get("state") in ("sending", "open", "partial"):
                self.s.setdefault("cancel_queue", []).append(
                    {"cid": o["cid"], "oid": o.get("oid"), "slot": o["slot"], "side": o["side"],
                     "price": o["price"], "qty": o["qty"], "gen": self.s.get("gen", 0)})
        self.orders = new

    def _new_order(self, slot, side, price, qty, active_from, kind="limit", tag=None):
        o = {"slot": slot, "side": side, "price": price, "qty": qty, "active_from": active_from}
        if self.mode == "testnet":
            seq = self.s["oseq"] = self.s.get("oseq", 0) + 1
            letter = tag or ("b" if side == "buy" else "s")
            o.update(type=kind, cid=f"{self.cid_prefix()}{slot}{letter}-{seq}", state="sending", oid=None,
                     exec_qty=0.0)
        return o

    def has_exchange_work(self):
        return self.mode == "testnet" and bool(self.s.get("cancel_queue") or
                                               any(o.get("state") == "sending" for o in self.orders))

    # ---------- arranque da grelha ----------
    def setup(self, ts, price):
        g = G.build_grid(price, self.capital, self.rules, self.p)
        self.grid = g
        s = self.s
        gen = s.get("gen", 0)
        self._set_orders([])                        # ordens da grelha anterior: cancelar (com o seu número de grelha)
        s.update(quote=g["budget"], reserve=self.capital - g["budget"], base=0.0, consec_buys=0, buys_blocked=None,
                 below_stop=0, above_upper=0, recenters={}, closes=[price], last_close=price, cycles=0, fees=0.0,
                 realized=0.0, worst_cycle=0.0, worst_loss_pct=0.0, initial_capital=self.capital,
                 day=_date(ts), day_start_equity=self.capital, started_ts=s.get("started_ts", ts),
                 stop_events=0, wins=0, gen=gen + 1, refused=[], start_price=s.get("start_price", price))
        if self.mode == "testnet":
            # compra inicial ao mercado: uma só ordem; a execução real reparte-se pelos degraus acima do preço
            init = [sl for sl in g["slots"] if g["prices"][sl["i"]] >= price]
            total = G.round_down(sum(sl["qty"] for sl in init), self.rules.get("step"))
            if init and total > 0:
                o = self._new_order(-1, "buy", price, total, ts, kind="market", tag="i")
                o["slots"] = [sl["i"] for sl in init]
                self.orders.append(o)
            init = []
        else:
            init = g["slots"]
        for sl in init:                             # degraus acima do preço: já com moeda (compra inicial ao mercado)
            if g["prices"][sl["i"]] >= price:
                fill = price * (1 + G.SLIP)
                cost = sl["qty"] * fill
                fee = cost * G.FEE
                if s["quote"] >= cost + fee:
                    s["quote"] -= cost + fee
                    s["base"] += sl["qty"]
                    s["fees"] += fee
                    sl["holding"], sl["buy_cost"] = True, cost + fee
                    self.new_fills.append({"ts": ts, "slot": sl["i"], "side": "buy", "price": fill,
                                           "qty": sl["qty"], "fee": fee, "pnl": None})
        self.status = RUNNING
        self._event(ts, "setup", f"Grelha montada em {self.pair}: {g['levels']} degraus entre {g['lower']:.6g} e "
                                 f"{g['upper']:.6g}, stop a {g['stop']:.6g}, degrau mínimo {g['step_pct']:.2f}%.")
        self._sync_orders(price, ts + MIN)

    # ---------- ordens ----------
    def _sync_orders(self, close, active_from):
        if not self.grid:
            return
        s, g = self.s, self.grid
        refused = set(s.get("refused") or [])
        keep = []
        reserved = sum(o["qty"] * o["price"] * (1 + G.SLIP) * (1 + G.FEE) for o in self.orders if o["side"] == "buy")
        pending_qty = sum(o["qty"] for o in self.orders if o["side"] == "buy")
        for o in self.orders:
            if o["slot"] < 0:
                continue
            sl = g["slots"][o["slot"]]
            if (o["side"] == "sell" and sl["holding"]) or (o["side"] == "buy" and not sl["holding"]
                                                          and self.status == RUNNING and not s["buys_blocked"]):
                keep.append(o)
        keep += [o for o in self.orders if o["slot"] < 0]        # compra inicial / liquidação em curso
        self._set_orders(keep)
        for sl in g["slots"]:
            i = sl["i"]
            if sl["holding"]:
                if not self._open(i, "sell") and f"{i}:sell" not in refused:
                    self.orders.append(self._new_order(i, "sell", g["prices"][i + 1], self._held(sl), active_from))
            elif self.status == RUNNING and not s["buys_blocked"] and not self._open(i, "buy") \
                    and f"{i}:buy" not in refused:
                price = g["prices"][i]
                cost = sl["qty"] * price * (1 + G.SLIP) * (1 + G.FEE)
                inv_after = (s["base"] + pending_qty + sl["qty"]) * close
                if price < close and s["quote"] - reserved >= cost and \
                        inv_after <= self.p["inventory_limit_pct"] / 100 * self.capital:
                    self.orders.append(self._new_order(i, "buy", price, sl["qty"], active_from))
                    reserved += cost
                    pending_qty += sl["qty"]

    # ---------- execuções ----------
    def _fill(self, o, ts):
        """Simulador: decide o preço e a comissão. Só corre no modo sim."""
        s = self.s
        if o["side"] == "buy":
            fill = o["price"] * (1 + G.SLIP)
            cost = o["qty"] * fill
            fee = cost * G.FEE
            if s["quote"] < cost + fee:
                return False
        else:
            fill = o["price"] * (1 - G.SLIP)
            fee = o["qty"] * fill * G.FEE
        self._apply_fill(o, ts, fill, o["qty"], fee)
        return True

    def _apply_fill(self, o, ts, fill, qty, fee, base_fee=0.0, partial=False, cash_fee=None):
        """Aplica uma execução (simulada ou real) a um degrau.

        `fee`: comissão em USDT (para o lucro). `base_fee`: parte paga em moeda. `cash_fee`: o que de facto saiu do
        saldo em USDT (0 se a comissão foi paga em moeda ou em BNB); por omissão, o que resta da comissão.
        """
        s, sl = self.s, self.grid["slots"][o["slot"]]
        step, min_notional = self.rules.get("step"), self.rules["min_notional"]
        cf = (0.0 if base_fee else fee) if cash_fee is None else cash_fee
        if o["side"] == "buy":
            cost = qty * fill
            s["quote"] -= cost + cf
            s["base"] += qty - base_fee
            s["fees"] += fee
            self.new_fills.append({"ts": ts, "slot": o["slot"], "side": "buy", "price": fill, "qty": qty,
                                   "fee": fee, "pnl": None})
            if partial and qty * fill < min_notional:      # pó: fica em moeda, sem degrau nem ordem de venda
                pass
            else:
                s["consec_buys"] += 1
                sl["holding"], sl["buy_cost"] = True, cost + fee
                if self.mode == "testnet":                 # a quantidade planeada (sl["qty"]) nunca muda
                    sl["hold_qty"] = G.round_down(qty - base_fee, step)
        else:
            proceeds = qty * fill
            held = self._held(sl)
            if partial and qty < held - 1e-12:              # venda cancelada a meio: só parte do degrau vendido
                part_cost = sl["buy_cost"] * qty / held
                pnl = proceeds - fee - part_cost
                s["quote"] += proceeds - cf
                s["base"] -= qty
                s["fees"] += fee
                s["realized"] += pnl
                sl["hold_qty"] = G.round_down(held - qty, step)
                sl["buy_cost"] -= part_cost
                if sl["hold_qty"] * fill < min_notional:    # o resto é pó: liberta o degrau
                    s["realized"] -= sl["buy_cost"]
                    sl["holding"], sl["buy_cost"] = False, 0.0
                    sl.pop("hold_qty", None)
                self.new_fills.append({"ts": ts, "slot": o["slot"], "side": "sell", "price": fill, "qty": qty,
                                       "fee": fee, "pnl": pnl})
            else:
                pnl = proceeds - fee - sl["buy_cost"]
                s["quote"] += proceeds - cf
                s["base"] -= qty
                s["fees"] += fee
                s["realized"] += pnl
                s["cycles"] += 1
                s["wins"] += 1 if pnl > 0 else 0
                s["worst_cycle"] = min(s["worst_cycle"], pnl)
                s["consec_buys"] = 0
                if s["buys_blocked"]:
                    self._event(ts, "info", "Houve uma venda: as compras voltam a ser permitidas.")
                    s["buys_blocked"] = None
                sl["holding"], sl["buy_cost"] = False, 0.0
                sl.pop("hold_qty", None)
                self.new_fills.append({"ts": ts, "slot": o["slot"], "side": "sell", "price": fill, "qty": qty,
                                       "fee": fee, "pnl": pnl})
        if o in self.orders:
            self.orders.remove(o)

    # ---------- execuções vindas da exchange (modo testnet) ----------
    def on_exchange_fill(self, o, ts, price, qty, fee, base_fee=0.0, partial=False, cash_fee=None):
        """Uma ordem foi executada (toda ou em parte) na Testnet, com preço e comissão reais."""
        if o.get("gen") is not None and o["gen"] != self.s.get("gen", 0):
            self._apply_orphan_fill(o, ts, price, qty, fee, base_fee, cash_fee)   # degrau de uma grelha que já não existe
        elif o["slot"] == -1 and o["side"] == "buy":
            self._apply_initial_buy(o, ts, price, qty, fee, base_fee, cash_fee)
        elif o["slot"] == -1:
            self._apply_liquidation(o, ts, price, qty, fee, cash_fee)
        else:
            self._apply_fill(o, ts, price, qty, fee, base_fee, partial, cash_fee)
        if self.status in (RUNNING, PAUSED) and self.grid:
            self._sync_orders(self.s["last_close"], ts)

    def _apply_orphan_fill(self, o, ts, price, qty, fee, base_fee, cash_fee):
        """Execução de uma ordem de uma grelha anterior (recentragem): conta no saldo, mas já não tem degrau."""
        s = self.s
        cf = (0.0 if base_fee else fee) if cash_fee is None else cash_fee
        if o["side"] == "buy":
            s["quote"] -= qty * price + cf
            s["base"] += qty - base_fee
        else:
            s["quote"] += qty * price - cf
            s["base"] = max(0.0, s["base"] - qty)
        s["fees"] += fee
        self.new_fills.append({"ts": ts, "slot": -2, "side": o["side"], "price": price, "qty": qty, "fee": fee,
                               "pnl": None})
        self._event(ts, "info", f"Execução tardia de uma ordem da grelha anterior ({o['side']} {qty:g}): "
                                "contada no saldo como pó.")
        if o in self.orders:
            self.orders.remove(o)

    def _apply_initial_buy(self, o, ts, price, qty, fee, base_fee, cash_fee=None):
        s, g = self.s, self.grid
        step, min_notional = self.rules.get("step"), self.rules["min_notional"]
        cf = (0.0 if base_fee else fee) if cash_fee is None else cash_fee
        cost = qty * price
        s["quote"] -= cost + cf
        received = qty - base_fee
        s["base"] += received
        s["fees"] += fee
        planned = sum(g["slots"][i]["qty"] for i in o["slots"]) or 1.0
        for i in o["slots"]:
            sl = g["slots"][i]
            share = sl["qty"] / planned
            q_i = G.round_down(received * share, step)
            if q_i * g["prices"][i] < min_notional:
                continue                                         # sem valor suficiente para um degrau
            sl["holding"], sl["buy_cost"], sl["hold_qty"] = True, (cost + fee) * share, q_i
            self.new_fills.append({"ts": ts, "slot": i, "side": "buy", "price": price, "qty": q_i,
                                   "fee": fee * share, "pnl": None})
        if o in self.orders:
            self.orders.remove(o)

    def _apply_liquidation(self, o, ts, price, qty, fee, cash_fee=None):
        s = self.s
        cf = fee if cash_fee is None else cash_fee
        proceeds = qty * price
        held = [sl for sl in self.grid["slots"] if sl["holding"]]
        cost = sum(sl["buy_cost"] for sl in held)
        s["quote"] += proceeds - cf
        s["fees"] += fee
        s["base"] = max(0.0, s["base"] - qty)
        pnl = proceeds - fee - cost
        s["realized"] += pnl
        total_qty = sum(self._held(sl) for sl in held) or 1.0
        for sl in held:
            share = self._held(sl) / total_qty
            loss = (sl["buy_cost"] - (proceeds - fee) * share) / self.capital * 100
            s["worst_loss_pct"] = max(s["worst_loss_pct"], loss)
            sl["holding"], sl["buy_cost"] = False, 0.0
            sl.pop("hold_qty", None)
        self.new_fills.append({"ts": ts, "slot": -1, "side": "sell", "price": price, "qty": qty, "fee": fee,
                               "pnl": pnl})
        if o in self.orders:
            self.orders.remove(o)

    def fail_order(self, o, ts, reason, stop=False):
        """A exchange ou a guarda recusaram uma ordem: retira-a, não a volta a criar e pára em segurança."""
        if o in self.orders:
            self.orders.remove(o)
        if o["slot"] >= 0:
            self.s.setdefault("refused", []).append(f"{o['slot']}:{o['side']}")   # não repete a mesma ordem a cada minuto
        self._event(ts, "guard", f"Ordem recusada ({o['side']} degrau {o['slot']}): {reason}")
        if stop:
            self.status, self.reason = STOPPED, reason
        else:
            self._pause(ts, f"ordem recusada: {reason}")

    def testnet_reset(self, ts, detail):
        """A Testnet foi reposta: o estado local já não corresponde à exchange. Pára e espera pelo utilizador."""
        self.orders = []
        self.s["cancel_queue"] = []
        self.s["testnet_reset"] = True
        self.status = STOPPED
        self.reason = "reset da Testnet detetado. Cria um bot novo para recomeçar (a janela de 7 dias reinicia)."
        self._event(ts, "reset", f"Reset da Testnet: {detail}")

    # ---------- paragens e pausas ----------
    def _below_cost(self, close):
        """Vender já toda a posição daria prejuízo (custo real dos degraus contra o preço, já sem comissão)?"""
        held = [sl for sl in self.grid["slots"] if sl["holding"]]
        if not held:
            return False
        cost = sum(sl["buy_cost"] for sl in held)
        qty = sum(self._held(sl) for sl in held)
        return qty * close * (1 - G.SLIP) * (1 - G.FEE) < cost

    def _hold_position(self, ts, reason):
        """Regra 'nunca vender abaixo do custo': cancela as ordens, mas mantém a moeda em vez de a vender."""
        self._set_orders([])
        self.status = STOPPED
        self.reason = (f"{reason}. Posição mantida: a regra 'nunca vender abaixo do custo' está ligada e vender "
                       "agora dava prejuízo.")
        self._event(ts, "stop", f"Bot parado: {self.reason}")

    def _liquidate(self, ts, close, reason):
        s = self.s
        if self.p.get("never_sell_below_cost") and self._below_cost(close):
            self._hold_position(ts, reason)
            return
        if self.mode == "testnet":
            self._set_orders([])                            # cancela tudo o que está na exchange (fila de cancelamentos)
            qty = G.round_down(s["base"], self.rules.get("step"))
            if qty > 0:
                self.orders.append(self._new_order(-1, "sell", close, qty, ts, kind="market", tag="l"))
            self.status, self.reason = STOPPED, reason
            self._event(ts, "stop", f"Bot parado: {reason}. A cancelar ordens e a fechar a posição na Testnet.")
            return
        self.orders = []
        if s["base"] > 1e-12:
            fill = close * (1 - G.SLIP)
            proceeds = s["base"] * fill
            fee = proceeds * G.FEE
            cost = sum(sl["buy_cost"] for sl in self.grid["slots"] if sl["holding"])
            loss_slots = [sl for sl in self.grid["slots"] if sl["holding"]]
            s["quote"] += proceeds - fee
            s["fees"] += fee
            pnl = proceeds - fee - cost
            s["realized"] += pnl
            for sl in loss_slots:                       # perda por operação (por degrau), para verificar o limite
                share = sl["qty"] / (sum(x["qty"] for x in loss_slots) or 1)
                loss = (sl["buy_cost"] - (proceeds - fee) * share) / self.capital * 100
                s["worst_loss_pct"] = max(s["worst_loss_pct"], loss)
                sl["holding"], sl["buy_cost"] = False, 0.0
            s["base"] = 0.0
            self.new_fills.append({"ts": ts, "slot": -1, "side": "sell", "price": fill, "qty": 0.0, "fee": fee,
                                   "pnl": pnl})
        self.status, self.reason = STOPPED, reason
        self._event(ts, "stop", f"Bot parado: {reason}. Posição fechada e ordens canceladas (simulação).")

    def _pause(self, ts, reason):
        if self.status != RUNNING:
            return
        self.status, self.reason = PAUSED, reason
        self._cancel_buys()
        self._event(ts, "pause", f"Bot em pausa: {reason}. Compras canceladas; as vendas abertas mantêm-se.")

    def command(self, cmd, ts, close=None):
        """Comandos vindos do painel: pause, resume, stop."""
        s = self.s
        if cmd == "pause" and self.status == RUNNING:
            self._pause(ts, "pedido do utilizador")
        elif cmd == "resume" and self.status == PAUSED:
            self.status, self.reason = RUNNING, ""
            s["buys_blocked"], s["consec_buys"], s["below_stop"], s["refused"] = None, 0, 0, []
            s["day_start_equity"] = self.equity(close or s["last_close"])
            self._event(ts, "info", "Bot retomado pelo utilizador.")
            self._sync_orders(close or s["last_close"], ts + MIN)
        elif cmd == "stop" and self.status in (RUNNING, PAUSED) and self.grid:
            self._liquidate(ts, close or s["last_close"], "pedido do utilizador")
        elif cmd == "stop" and self.status == PENDING:
            self.status, self.reason = STOPPED, "pedido do utilizador"

    # ---------- uma vela ----------
    def process_candle(self, candle):
        ts, o, h, l, c = candle
        if self.status != STOPPED:
            self.new_candles.append(candle)         # guardadas para o gráfico do bot
        if self.status == PENDING:
            self.setup(ts, c)
            self.last_ts = ts
            return
        if self.status == STOPPED:
            self.last_ts = ts
            return
        s, g = self.s, self.grid
        day = _date(ts)
        if day != s["day"]:
            s["day"], s["day_start_equity"] = day, self.equity(s["last_close"])

        # 1) execuções, por ordem de distância ao fecho anterior; nunca um ciclo completo numa só vela
        prev = s["last_close"]
        due = [] if self.mode == "testnet" else sorted((x for x in self.orders if x["active_from"] <= ts),
                                                       key=lambda x: abs(x["price"] - prev))
        for x in due:
            hit = l <= x["price"] * (1 - G.FILL_MARGIN) if x["side"] == "buy" else h >= x["price"] * (1 + G.FILL_MARGIN)
            if hit and x in self.orders and self._fill(x, ts):
                self._sync_orders(c, ts + MIN)
        if s["consec_buys"] >= self.p["max_consecutive_buys"] and not s["buys_blocked"]:
            s["buys_blocked"] = f"{s['consec_buys']} compras seguidas sem venda"
            self._cancel_buys()
            self._event(ts, "blocked", f"Compras suspensas: {s['buys_blocked']} (sinal de tendência de queda).")

        s["closes"] = (s["closes"] + [c])[-(self.p["drop_window_minutes"] + 1):]
        s["last_close"] = c
        eq = self.equity(c)

        # 2) stop-loss: fecho abaixo do stop durante X minutos seguidos
        s["below_stop"] = s["below_stop"] + 1 if c < g["stop"] else 0
        if s["below_stop"] >= self.p["stop_minutes"]:
            if self.p.get("never_sell_below_cost") and self._below_cost(c):
                s["below_stop"] = 0                 # a regra impede a venda: pausa, mantém a posição e as vendas abertas
                s["stop_events"] += 1
                self._pause(ts, f"stop-loss atingido, mas a regra 'nunca vender abaixo do custo' mantém a posição")
            else:
                s["stop_events"] += 1
                self._liquidate(ts, c, f"stop-loss ({s['below_stop']} min abaixo de {g['stop']:.6g})")
                self._finish(ts, c)
                return

        # 3) pausas de proteção
        top = max(s["closes"])
        if self.status == RUNNING and (top - c) / top * 100 >= self.p["drop_pause_pct"]:
            self._pause(ts, f"queda de {(top - c) / top * 100:.1f}% em {self.p['drop_window_minutes']} minutos")
        if self.status == RUNNING and eq <= s["day_start_equity"] * (1 - self.p["daily_loss_pct"] / 100):
            self._pause(ts, f"limite diário de perda ({self.p['daily_loss_pct']:g}%) atingido")
        if self.status == RUNNING and eq <= s["initial_capital"] * (1 - self.p["pause_drawdown_pct"] / 100):
            self._pause(ts, f"o capital do bot caiu {self.p['pause_drawdown_pct']:g}%")

        # 4) acima do intervalo: recentrar no máximo 1 vez por evento e N por dia
        s["above_upper"] = s["above_upper"] + 1 if c > g["upper"] else 0
        if self.status == RUNNING and s["above_upper"] >= self.p["upper_wait_minutes"]:
            done = s["recenters"].get(day, 0)
            if self._is_flat(c) and done < self.p["max_recenter_per_day"]:
                s["recenters"][day] = done + 1
                keep = {k: s[k] for k in ("cycles", "fees", "realized", "worst_cycle", "worst_loss_pct", "wins",
                                          "stop_events", "recenters", "started_ts", "initial_capital",
                                          "day", "day_start_equity", "start_price", "uid", "oseq")
                        if k in s}
                dust = s["base"]                   # pó que sobrou (abaixo da ordem mínima): continua a ser nosso
                cash_total = s["quote"] + s["reserve"]
                original = self.capital
                self.capital = cash_total          # a nova grelha usa o dinheiro que sobrou (com lucros/perdas)
                self.setup(ts, c)
                self.capital = original
                s.update(keep)
                s["base"] = dust
                s["reserve"] = cash_total - self.grid["budget"]
                self._event(ts, "recenter", f"Grelha recentrada em {c:.6g} ({done + 1}.ª vez hoje).")
            s["above_upper"] = 0

        if ts % 3_600_000 == 0:
            self.new_equity.append({"ts": ts, "equity": eq})
        self._sync_orders(c, ts + MIN)
        self.last_ts = ts

    def _finish(self, ts, close):
        self.new_equity.append({"ts": ts, "equity": self.equity(close)})
        self.last_ts = ts
