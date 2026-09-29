"""Motor do bot de grelha. Nunca fala com a Binance: recebe velas (e, no modo testnet, execuções) e decide.

Estado inteiro serializável: reiniciar o processo retoma exatamente onde ficou, sem duplicar ordens.
Vela: (abertura_ms, open, high, low, close).

Modo "sim": o simulador decide quando as ordens executam. Modo "testnet": as ordens saem para a Testnet por uma camada
à parte e as execuções reais chegam por `on_exchange_fill`; este ficheiro só decide e emite intenções.
"""
import re
import secrets
from datetime import datetime, timezone

from . import grid as G

MIN = 60_000
RUNNING, PAUSED, STOPPED, PENDING = "running", "paused", "stopped", "pending"
RECOVERING = "recovering"     # a reconciliar com a exchange: não cria ordens novas até os invariantes se cumprirem
STOPPING = "stopping"         # paragem pedida: só cancela, contabiliza e fecha; STOPPED só depois de confirmado na exchange
# como terminou uma paragem (bot STOPPED): posição fechada / pó que não dá para vender / posição mantida pela regra do custo
STOP_CLOSED, STOP_RESIDUAL, STOP_POSITION_KEPT = "closed", "residual", "position_kept"


def parse_cid(cid):
    """(id do bot, uid, contador) de um id nosso (cb{bot}{uid de 6 hex}-{degrau}{lado}-{contador}); None se for de outro."""
    if not isinstance(cid, str) or not cid.startswith("cb") or "-" not in cid:
        return None
    head, _, rest = cid[2:].partition("-")
    bot, uid, seq = head[:-6], head[-6:], rest.rsplit("-", 1)[-1]
    if not (bot.isdigit() and seq.isdigit() and re.fullmatch(r"[0-9a-f]{6}", uid)):
        return None
    return int(bot), uid, int(seq)


def cid_slot(cid):
    """(degrau, letra) do id de uma ordem nossa: -1 = compra inicial/liquidação; letra b=compra, s=venda, i/l=mercado."""
    m = re.fullmatch(r"(-?\d+)([a-z])-\d+", cid.partition("-")[2]) if isinstance(cid, str) else None
    return (int(m.group(1)), m.group(2)) if m else None


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
        self.seen = bot.get("seen") or {}              # registo de trades: id da ordem -> {id do trade: execução}
        self.new_trades = []                           # trades novos deste passo (gravados junto com o estado)
        self.findings = []                             # classificação do último reconcile (só em memória)

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

    def uids(self):
        """Todos os UID que reconhecemos como deste bot: o atual e os antigos ligados a ele (restauro de cópias)."""
        self.cid_prefix()
        return {self.s["uid"], *(self.s.get("old_uids") or [])}

    def link_uid(self, uid):
        """Liga um UID antigo a este bot: as suas ordens passam a ser reconhecidas (e limpas) na recuperação."""
        if uid != self.s.get("uid") and uid not in self.s.setdefault("old_uids", []):
            self.s["old_uids"].append(uid)

    def owns(self, cid):
        """A ordem é deste bot? Só se o id tiver o nosso número de bot E um UID que reconhecemos. Nunca só pelo prefixo."""
        p = parse_cid(cid)
        return bool(p) and p[0] == (self.id or 0) and p[1] in self.uids()

    def note_cid(self, cid):
        """Um id nosso já existe na exchange: o contador nunca pode voltar atrás dele (cópias antigas repetiam ids)."""
        p = parse_cid(cid)
        if p and self.owns(cid):
            self.s["oseq"] = max(self.s.get("oseq", 0), p[2])

    def fits_grid(self, slot, side, price):
        """Uma ordem antiga (recuperada) ainda corresponde a este degrau da grelha atual? Degrau, lado e preço têm de bater."""
        g = self.grid
        if not g or not 0 <= slot < len(g["slots"]):
            return False
        sl, tol = g["slots"][slot], max(self.rules.get("tick") or 0.0, 1e-9)
        if side == "sell":
            return bool(sl["holding"]) and abs(price - g["prices"][slot + 1]) <= tol
        return not sl["holding"] and abs(price - g["prices"][slot]) <= tol

    def rekey(self, o):
        """Dá um id novo a uma ordem cujo id já existe na exchange com outro conteúdo."""
        seq = self.s["oseq"] = self.s.get("oseq", 0) + 1
        o["cid"] = f"{o['cid'].rsplit('-', 1)[0]}-{seq}"

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
                                               any(o.get("state") in ("sending", "settling") for o in self.orders))

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
            if o["slot"] < 0 or o.get("state") == "settling":
                continue
            sl = g["slots"][o["slot"]]
            if (o["side"] == "sell" and sl["holding"]) or (o["side"] == "buy" and not sl["holding"]
                                                          and self.status == RUNNING and not s["buys_blocked"]):
                keep.append(o)
        keep += [o for o in self.orders if o["slot"] < 0 or o.get("state") == "settling"]   # compra inicial, liquidação, por reconciliar
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

    def _apply_fill(self, o, ts, fill, qty, fee, base_fee=0.0, partial=False, cash_fee=None, cash=True):
        """Aplica uma execução (simulada ou real) a um degrau.

        `fee`: comissão em USDT (para o lucro). `base_fee`: parte paga em moeda. `cash_fee`: o que de facto saiu do
        saldo em USDT (0 se a comissão foi paga em moeda ou em BNB); por omissão, o que resta da comissão.
        `cash=False`: o dinheiro, a moeda e as comissões já foram contados trade a trade (apply_trades): falta o degrau.
        """
        s, sl = self.s, self.grid["slots"][o["slot"]]
        step, min_notional = self.rules.get("step"), self.rules["min_notional"]
        cf = (0.0 if base_fee else fee) if cash_fee is None else cash_fee
        if o["side"] == "buy":
            cost = qty * fill
            if cash:
                s["quote"] -= cost + cf
                s["base"] += qty - base_fee
                s["fees"] += fee
            self.new_fills.append({"ts": ts, "slot": o["slot"], "side": "buy", "price": fill, "qty": qty,
                                   "fee": fee, "pnl": None})
            if partial and qty * fill < min_notional:      # pó: fica em moeda, sem degrau nem ordem de venda
                self._residual_add(qty - base_fee, cost + fee)
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
                if cash:
                    s["quote"] += proceeds - cf
                    s["base"] -= qty
                    s["fees"] += fee
                s["realized"] += pnl
                sl["hold_qty"] = G.round_down(held - qty, step)
                sl["buy_cost"] -= part_cost
                if sl["hold_qty"] * fill < min_notional:    # o resto é pó: liberta o degrau, mas o custo não se perde
                    self._residual_add(sl["hold_qty"], sl["buy_cost"])
                    sl["holding"], sl["buy_cost"] = False, 0.0
                    sl.pop("hold_qty", None)
                self.new_fills.append({"ts": ts, "slot": o["slot"], "side": "sell", "price": fill, "qty": qty,
                                       "fee": fee, "pnl": pnl})
            else:
                pnl = proceeds - fee - sl["buy_cost"]
                if cash:
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

    # ---------- pó / posição residual (estado explícito) ----------
    def _residual_add(self, qty, cost):
        """Moeda que ficou sem degrau (abaixo da ordem mínima): continua nossa, com o seu custo, e não é perda realizada."""
        if qty > 0:
            self.s["residual_qty"] = self.s.get("residual_qty", 0.0) + qty
            self.s["residual_cost"] = self.s.get("residual_cost", 0.0) + cost

    def _residual_take(self, qty):
        """Vendeu-se moeda que não pertencia a nenhum degrau: sai do residual. Devolve o custo que saiu com ela."""
        s = self.s
        have = s.get("residual_qty", 0.0)
        if have <= 0 or qty <= 0:
            return 0.0
        take = min(have, qty)
        cost = s.get("residual_cost", 0.0) * take / have
        left = have - take
        if left > 1e-12:
            s["residual_qty"], s["residual_cost"] = left, s.get("residual_cost", 0.0) - cost
        else:
            s["residual_qty"], s["residual_cost"] = 0.0, 0.0
        return cost

    def position(self):
        """A posição do bot em partes, para saber o que se pode fechar e o que não (fase 2).

        total = moeda que o bot julga ter; tradable = a que está em degraus com venda possível; residual = pó declarado
        (com custo); unattributed = o que sobra (pequeno = arredondamentos; grande = posição desconhecida).
        """
        s = self.s
        total = max(s.get("base", 0.0), 0.0)
        slots = [sl for sl in (self.grid or {}).get("slots", []) if sl["holding"]]
        tradable = sum(self._held(sl) for sl in slots)
        residual = s.get("residual_qty", 0.0)
        unattributed = total - tradable - residual
        tol = (len(slots) + 2) * (self.rules.get("step") or 0.0) + 1e-9
        if total <= 1e-12:
            kind = "flat"
        elif abs(unattributed) > tol:
            kind = "unknown"
        elif tradable > 0:
            kind = "closable"
        elif residual > 0:
            kind = "residual"
        else:
            kind = "flat"
        return {"kind": kind, "total_qty": total, "tradable_qty": tradable, "residual_qty": residual,
                "residual_cost": s.get("residual_cost", 0.0), "unattributed_qty": unattributed}

    # ---------- execuções vindas da exchange (modo testnet) ----------
    def register_trades(self, cid, order_id, side, rows, ts, status="booked"):
        """Regista os trades ainda não contabilizados de uma ordem: cada trade entra uma só vez (id + ordem).

        `rows`: execuções normalizadas (id, qty, price, quoteQty, commission, commissionAsset, time). Devolve as novas.
        """
        seen, new = self.seen.setdefault(cid, {}), []
        for r in rows:
            tid = str(r["id"])
            if tid in seen:
                continue
            seen[tid] = r
            self.new_trades.append({"symbol": self.pair, "cid": cid, "order_id": None if order_id is None else str(order_id),
                                    "trade_id": tid, "side": side, "qty": float(r["qty"]), "price": float(r["price"]),
                                    "quote_qty": float(r["quoteQty"]), "commission": float(r.get("commission") or 0.0),
                                    "commission_asset": r.get("commissionAsset"), "trade_time": r.get("time"),
                                    "processed_at": ts, "status": status})
            new.append(r)
        return new

    def booked(self, cid):
        """Quantidade já contabilizada de uma ordem (soma dos trades registados)."""
        return sum(float(r["qty"]) for r in self.seen.get(cid, {}).values())

    def apply_trades(self, o, ts, qty, quote, fee, base_fee, cash_fee):
        """Conta já (dinheiro, moeda, comissões) uma parte das execuções de uma ordem. O degrau só muda no fim."""
        s = self.s
        if o["side"] == "buy":
            s["quote"] -= quote + cash_fee
            s["base"] += qty - base_fee
        else:
            s["quote"] += quote - cash_fee
            s["base"] -= qty
            if s["base"] < -1e-9:
                self._event(ts, "guard", f"A Testnet vendeu mais moeda ({qty:g}) do que o bot julgava ter: ficou a "
                                         f"{s['base']:g}. A contabilidade precisa de revisão.")
            s["base"] = max(0.0, s["base"])
        s["fees"] += fee
        if o.get("cid"):
            o["exec_qty"] = max(o.get("exec_qty", 0.0), self.booked(o["cid"]))

    def _dispatch(self, o, ts, price, qty, fee, base_fee, partial, cash_fee, cash):
        if o.get("gen") is not None and o["gen"] != self.s.get("gen", 0):
            self._apply_orphan_fill(o, ts, price, qty, fee, base_fee, cash_fee, cash)   # degrau de uma grelha que já não existe
        elif o["slot"] == -1 and o["side"] == "buy":
            self._apply_initial_buy(o, ts, price, qty, fee, base_fee, cash_fee, cash)
        elif o["slot"] == -1:
            self._apply_liquidation(o, ts, price, qty, fee, cash_fee, cash)
        else:
            self._apply_fill(o, ts, price, qty, fee, base_fee, partial, cash_fee, cash)
        if self.status in (RUNNING, PAUSED) and self.grid:
            self._sync_orders(self.s["last_close"], ts)

    def on_exchange_fill(self, o, ts, price, qty, fee, base_fee=0.0, partial=False, cash_fee=None):
        """Uma ordem foi executada (toda ou em parte) na Testnet, com preço e comissão reais (tudo de uma vez)."""
        self._dispatch(o, ts, price, qty, fee, base_fee, partial, cash_fee, cash=True)

    def finish_order(self, o, ts, partial, qty, quote, fee, base_fee, cash_fee):
        """A ordem terminou e todos os seus trades já foram contados: fecha o degrau (posição, ciclo, pó)."""
        if qty <= 0:
            if o in self.orders:
                self.orders.remove(o)
            return
        self._dispatch(o, ts, quote / qty, qty, fee, base_fee, partial, cash_fee, cash=False)

    def _apply_orphan_fill(self, o, ts, price, qty, fee, base_fee, cash_fee, cash=True):
        """Execução de uma ordem de uma grelha anterior (recentragem): conta no saldo, mas já não tem degrau."""
        s = self.s
        cf = (0.0 if base_fee else fee) if cash_fee is None else cash_fee
        if o["side"] == "buy":
            if cash:
                s["quote"] -= qty * price + cf
                s["base"] += qty - base_fee
            self._residual_add(qty - base_fee, qty * price + fee)
        else:
            if cash:
                s["quote"] += qty * price - cf
                s["base"] = max(0.0, s["base"] - qty)
            self._residual_take(qty)
        if cash:
            s["fees"] += fee
        self.new_fills.append({"ts": ts, "slot": -2, "side": o["side"], "price": price, "qty": qty, "fee": fee,
                               "pnl": None})
        self._event(ts, "info", f"Execução tardia de uma ordem da grelha anterior ({o['side']} {qty:g}): "
                                "contada no saldo como pó.")
        if o in self.orders:
            self.orders.remove(o)

    def _apply_initial_buy(self, o, ts, price, qty, fee, base_fee, cash_fee=None, cash=True):
        s, g = self.s, self.grid
        step, min_notional = self.rules.get("step"), self.rules["min_notional"]
        cf = (0.0 if base_fee else fee) if cash_fee is None else cash_fee
        cost = qty * price
        received = qty - base_fee
        if cash:
            s["quote"] -= cost + cf
            s["base"] += received
            s["fees"] += fee
        planned = sum(g["slots"][i]["qty"] for i in o["slots"]) or 1.0
        for i in o["slots"]:
            sl = g["slots"][i]
            share = sl["qty"] / planned
            q_i = G.round_down(received * share, step)
            if q_i * g["prices"][i] < min_notional:
                self._residual_add(received * share, (cost + fee) * share)   # sem valor para um degrau: fica como pó
                continue
            sl["holding"], sl["buy_cost"], sl["hold_qty"] = True, (cost + fee) * share, q_i
            self.new_fills.append({"ts": ts, "slot": i, "side": "buy", "price": price, "qty": q_i,
                                   "fee": fee * share, "pnl": None})
        if o in self.orders:
            self.orders.remove(o)

    def _apply_liquidation(self, o, ts, price, qty, fee, cash_fee=None, cash=True):
        s = self.s
        cf = fee if cash_fee is None else cash_fee
        proceeds = qty * price
        held = [sl for sl in self.grid["slots"] if sl["holding"]]
        cost = sum(sl["buy_cost"] for sl in held)
        cost += self._residual_take(max(0.0, qty - sum(self._held(sl) for sl in held)))   # o pó vendido leva o seu custo
        if cash:
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
        """A recuperação concluiu que a Testnet foi reposta: a posição do bot já não existe lá. Pára e espera pelo utilizador.

        Só limpa as ordens que a exchange já não conhece. Execuções, registo de trades, lucro realizado, custos e eventos
        ficam como estão: nada financeiro se apaga.
        """
        self._set_orders([])                                # o que ainda pudesse estar aberto vai para a fila de cancelamentos
        self.s.pop("recovery", None)
        self.s["testnet_reset"] = True
        self.status = STOPPED
        self.reason = "reset da Testnet detetado. Cria um bot novo para recomeçar (a janela de 7 dias reinicia)."
        self._event(ts, "reset", f"Reset da Testnet: {detail}")

    # ---------- recuperação (RECOVERING) ----------
    def enter_recovery(self, ts, reasons):
        """Passa a RECOVERING: nenhuma ordem nova até o estado local voltar a bater com a exchange."""
        rec = self.s.get("recovery")
        if self.status == RECOVERING and rec:
            rec["reasons"] = sorted(set(rec["reasons"]) | set(reasons))
            return
        self.s["recovery"] = {"since": ts, "from": self.status, "from_reason": self.reason, "reasons": sorted(set(reasons)),
                              "cmd": None}
        self.status = RECOVERING
        self.reason = "a reconciliar com a Testnet (" + "; ".join(sorted(set(reasons))) + "). Sem ordens novas até terminar."
        self._event(ts, "recovery", f"Recuperação iniciada: {'; '.join(sorted(set(reasons)))}. Nenhuma ordem nova até terminar.")

    def finish_recovery(self, ts, summary):
        """Os invariantes cumprem-se: volta ao estado anterior, reconstrói as ordens e aplica o comando que chegou entretanto."""
        rec = self.s.pop("recovery", None) or {}
        self.status, self.reason = rec.get("from") or (RUNNING if self.grid else PENDING), rec.get("from_reason", "")
        if self.status == RECOVERING:
            self.status = RUNNING
        self.s["last_recovery"] = {"ts": ts, "summary": summary, "reasons": rec.get("reasons", [])}
        self._event(ts, "recovery", f"Recuperação concluída: {summary}.")
        cmd = rec.get("cmd")
        if self.grid and self.status in (RUNNING, PAUSED) and cmd != "stop":
            self._sync_orders(self.s["last_close"], ts + MIN)
        if cmd and self.grid and self.status in (RUNNING, PAUSED):
            self.command(cmd, ts, self.s["last_close"])
            if cmd == "stop" and rec.get("stop_reason"):
                self.reason = rec["stop_reason"]

    # ---------- paragem (STOPPING) ----------
    def request_stop(self, ts, reason, sell=True):
        """Testnet: começa a paragem. Nunca salta para STOPPED: fica STOPPING até a camada de ordens confirmar na exchange
        (sem ordens abertas do bot, sem ordens por enviar ou por reconciliar e posição fechada). Daqui em diante o motor
        já não cria ordens de estratégia. `sell=False`: só cancela (regra 'nunca vender abaixo do custo')."""
        if self.status in (STOPPING, STOPPED):
            return
        self.s.pop("recovery", None)                        # a paragem inclui a reconciliação completa
        self._set_orders([o for o in self.orders if o.get("state") == "settling"])   # o resto vai para a fila de cancelamentos
        self.s["stop"] = {"since": ts, "reason": reason, "sell": sell, "from": self.status, "overdue": 0, "attempts": 0}
        self.status, self.reason = STOPPING, reason
        self._event(ts, "stop", f"A parar: {reason}. A cancelar as ordens e a confirmar na Testnet antes de dar o bot como parado.")

    def _release_to_residual(self):
        """Posição que não se consegue vender (abaixo do mínimo): passa toda a moeda a residual, com o custo, sem perda."""
        s = self.s
        for sl in (self.grid or {}).get("slots", []):
            if sl["holding"]:
                self._residual_add(self._held(sl), sl["buy_cost"])
                sl["holding"], sl["buy_cost"] = False, 0.0
                sl.pop("hold_qty", None)
        extra = s["base"] - s.get("residual_qty", 0.0)
        if extra > 1e-12:
            self._residual_add(extra, 0.0)                  # sobra de arredondamentos: já teve o custo no lucro da venda
        if s["base"] <= 1e-9:
            s["residual_qty"] = s["residual_cost"] = 0.0

    def finish_stop(self, ts):
        """A exchange confirmou (sem ordens nem posição fechável): o bot passa a STOPPED e regista como terminou."""
        st = self.s.pop("stop", None) or {}
        if not st.get("sell", True):
            outcome = STOP_POSITION_KEPT
        else:
            self._release_to_residual()
            outcome = STOP_RESIDUAL if self.s.get("residual_qty", 0.0) > 1e-9 else STOP_CLOSED
        self.s["stop_outcome"] = outcome
        self.status = STOPPED
        self.reason = st.get("reason", self.reason)
        detail = {STOP_CLOSED: "posição fechada e ordens canceladas",
                  STOP_RESIDUAL: f"posição fechada; sobra {self.s.get('residual_qty', 0.0):g} de pó abaixo do mínimo "
                                 "(fica no bot, com o custo, sem contar como perda)",
                  STOP_POSITION_KEPT: "ordens canceladas; a moeda foi mantida"}[outcome]
        self._event(ts, "stop", f"Bot parado (confirmado na Testnet): {self.reason}. {detail[0].upper() + detail[1:]}.")

    def check_stop_overdue(self, now, deadline_ms):
        """Passou o prazo sem confirmação: alerta CRÍTICO (repete a cada prazo). O bot continua STOPPING, nunca STOPPED."""
        st = self.s.get("stop")
        if st and deadline_ms > 0 and now - st["since"] >= deadline_ms * (st["overdue"] + 1):
            st["overdue"] += 1
            self._event(now, "guard", f"CRÍTICO: a paragem ({st['reason']}) dura há {(now - st['since']) // 1000} s e ainda "
                                      "não está confirmada na Testnet. O bot NÃO está parado: continua a tentar.")

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
        kept = (f"{reason}. Posição mantida: a regra 'nunca vender abaixo do custo' está ligada e vender "
                "agora dava prejuízo.")
        if self.mode == "testnet":                          # cancela e confirma na exchange; não vende
            self.request_stop(ts, kept, sell=False)
            return
        self._set_orders([])
        self.status = STOPPED
        self.reason = kept
        self._event(ts, "stop", f"Bot parado: {self.reason}")

    def _liquidate(self, ts, close, reason):
        s = self.s
        if self.p.get("never_sell_below_cost") and self._below_cost(close):
            self._hold_position(ts, reason)
            return
        if self.mode == "testnet":
            self.request_stop(ts, reason)                   # a camada de ordens cancela, fecha a posição e confirma
            return
        self.orders = []
        if s["base"] > 1e-12:
            fill = close * (1 - G.SLIP)
            proceeds = s["base"] * fill
            fee = proceeds * G.FEE
            cost = sum(sl["buy_cost"] for sl in self.grid["slots"] if sl["holding"]) + s.get("residual_cost", 0.0)
            s["residual_qty"] = s["residual_cost"] = 0.0
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
        self.s["paused_buy_slots"] = sorted({o["slot"] for o in self.orders if o["side"] == "buy" and o["slot"] >= 0})
        self._cancel_buys()
        self._event(ts, "pause", f"Bot em pausa: {reason}. Compras canceladas; as vendas abertas mantêm-se.")

    def command(self, cmd, ts, close=None, reason=None):
        """Comandos vindos do painel: pause, resume, stop."""
        s = self.s
        if self.status == STOPPING:
            return                                          # já a parar: nada a acrescentar
        if self.status == RECOVERING:
            rec = s.get("recovery")
            if cmd == "stop" and self.grid:                 # a paragem inclui a reconciliação: não espera pelo fim da recuperação
                self.request_stop(ts, reason or "pedido do utilizador")
            elif rec is not None and cmd in ("pause", "resume"):
                rec["cmd"] = None if cmd == "resume" else cmd    # pausa/retoma guardam-se para o fim
            return
        if cmd == "pause" and self.status == RUNNING:
            self._pause(ts, "pedido do utilizador")
        elif cmd == "resume" and self.status == PAUSED:
            self.status, self.reason = RUNNING, ""
            s["buys_blocked"], s["consec_buys"], s["below_stop"], s["refused"] = None, 0, 0, []
            # day_start_equity NÃO se repõe aqui: senão o limite diário de perda (secção 2/5 do DESIGN.md) fica-se por
            # pausar-e-retomar repetidas vezes no mesmo dia sem nunca travar a perda a sério. Só a mudança de dia (acima,
            # em process_candle) ou a ativação de um bot parado reinicia este marcador.
            paused_slots = s.pop("paused_buy_slots", [])
            self._sync_orders(close or s["last_close"], ts + MIN)
            restored = {o["slot"] for o in self.orders if o["side"] == "buy"}
            missing = [i for i in paused_slots if i not in restored]
            if not paused_slots:
                self._event(ts, "info", "Bot retomado pelo utilizador.")
            elif missing:
                self._event(ts, "info", f"Bot retomado pelo utilizador. {len(missing)} de {len(paused_slots)} "
                                        f"compra(s) canceladas na pausa não foram repostas: o preço já passou esse(s) "
                                        f"degrau(s) {missing}.")
            else:
                self._event(ts, "info", "Bot retomado pelo utilizador. As compras canceladas na pausa foram todas repostas.")
        elif cmd == "stop" and self.status in (RUNNING, PAUSED) and self.grid:
            self._liquidate(ts, close or s["last_close"], reason or "pedido do utilizador")
        elif cmd == "stop" and self.status == PENDING:
            self.status, self.reason = STOPPED, reason or "pedido do utilizador"
        elif cmd == "activate":
            self.activate(ts)

    def _rebuild_grid(self, ts, c):
        """Monta uma grelha nova ao preço `c` sem perder nada do bot: contadores, custo do pó, ids e o dinheiro que
        sobrou. Devolve False (grelha antiga mantida, nada mudado) se a guarda de risco recusar a grelha nova a este
        preço — nunca deixa a exceção propagar e derrubar o passo do bot inteiro."""
        s = self.s
        keep = {k: s[k] for k in ("cycles", "fees", "realized", "worst_cycle", "worst_loss_pct", "wins",
                                  "stop_events", "recenters", "started_ts", "initial_capital",
                                  "day", "day_start_equity", "start_price", "uid", "oseq")
                if k in s}
        dust = s["base"]                   # pó que sobrou (abaixo da ordem mínima): continua a ser nosso
        cash_total = s["quote"] + s["reserve"]
        original = self.capital
        self.capital = cash_total          # a nova grelha usa o dinheiro que sobrou (com lucros/perdas)
        try:
            self.setup(ts, c)
        except G.GridRefused as exc:
            self._event(ts, "guard", f"Recentrar em {c:.6g} recusado pela guarda de risco: {exc} Grelha mantida.")
            return False
        finally:
            self.capital = original
        s.update(keep)
        # soma-se ao pó antigo (não substitui): setup() já zerou "base" e comprou, a mercado, os degraus da grelha
        # nova que ficam acima do preço atual — sobrescrever com só o pó antigo deitava fora essa compra a dinheiro
        # já gasto, fazendo o capital "desaparecer" da contabilidade num recentrar (achado real de uso).
        s["base"] += dust
        s["reserve"] = cash_total - self.grid["budget"]
        return True

    def activate(self, ts):
        """Ativação individual de um bot parado (botão ATIVAR). Nunca é automática. Devolve False se não puder.

        Só com a exchange confirmada (nada por cancelar, enviar ou reconciliar). Sem moeda por vender: grelha nova ao preço
        atual, guardando o histórico e o dinheiro. Com moeda por vender (ex.: parou pela regra do custo): retoma a grelha
        que tinha, com as vendas já abertas. Um reset da Testnet exige bot novo (a posição já não existe lá).
        """
        s = self.s
        if self.status != STOPPED or not self.grid or s.get("testnet_reset") or s.get("cancel_queue") \
                or any(o.get("state") in ("sending", "settling") for o in self.orders):
            return False
        for key in ("stop", "stop_outcome", "recovery"):
            s.pop(key, None)
        self.orders, self.reason, self.last_ts = [], "", 0          # last_ts 0: recomeça na última vela, sem repetir o passado
        s.update(refused=[], buys_blocked=None, consec_buys=0, below_stop=0, above_upper=0)
        if any(sl["holding"] for sl in self.grid["slots"]):
            self.status = RUNNING
            s["day_start_equity"] = self.equity(s["last_close"])
        else:
            s["reactivate"] = True                                  # a 1.ª vela monta a grelha nova
            self.status = PENDING
        self._event(ts, "info", "Bot ativado pelo utilizador.")
        return True

    # ---------- uma vela ----------
    def process_candle(self, candle):
        ts, o, h, l, c = candle
        if self.status in (RECOVERING, STOPPING):
            return                                          # as velas voltam a ser lidas quando a recuperação terminar
        if self.status != STOPPED:
            self.new_candles.append(candle)         # guardadas para o gráfico do bot
        if self.status == PENDING:
            if self.s.pop("reactivate", None) and self.grid:
                self._rebuild_grid(ts, c)
                self.s["day_start_equity"] = self.equity(c)
            else:
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
                if self._rebuild_grid(ts, c):          # só conta para o limite diário e anuncia se recentrou mesmo
                    s["recenters"][day] = done + 1
                    self._event(ts, "recenter", f"Grelha recentrada em {c:.6g} ({done + 1}.ª vez hoje).")
            s["above_upper"] = 0

        if ts % 3_600_000 == 0:
            self.new_equity.append({"ts": ts, "equity": eq})
        self._sync_orders(c, ts + MIN)
        self.last_ts = ts

    def _finish(self, ts, close):
        self.new_equity.append({"ts": ts, "equity": self.equity(close)})
        self.last_ts = ts
