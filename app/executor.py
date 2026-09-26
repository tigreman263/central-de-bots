"""Executor da Testnet: traduz as intenções do motor em ordens reais e reconcilia o estado com a exchange.

O motor (engine.py) decide e nunca fala com a Binance. Aqui a exchange é a fonte da verdade:
- `reconcile` corre ANTES de o motor avançar: lê as ordens abertas e as execuções e ajusta o estado local;
- `flush` corre DEPOIS de o estado ter sido gravado: cancela o que o motor retirou e envia o que ele decidiu.
Cada ordem leva um id único (bot+degrau+lado+contador) gravado ANTES do envio. Se o processo morrer a meio, no
arranque seguinte procura-se o id na exchange: se já existe, adota-se; se não, envia-se. Nunca há duplicados.
Só comunica através de `trader.Trader` (o único módulo com ordens) e todas as ordens passam por `grid.validate_order`.
"""
from . import grid as G
from .trader import DUPLICATE, UNKNOWN_ORDER, TraderError

BALANCE_CHECK_EVERY_MS = 3_600_000
RESET_BALANCE_RATIO = 0.5      # a Testnet tem menos de metade da moeda que o bot julga ter: foi reposta (ou mexeram-lhe)


def _assets(pair):
    if not pair.endswith("USDT"):
        raise TraderError("Só há suporte para pares em USDT na Testnet.")
    return pair[:-4], "USDT"


def summarize(pair, resp, trades, side=""):
    """(preço médio, quantidade, comissão em USDT, comissão paga em moeda, comissão saída do saldo em USDT)."""
    base, quote = _assets(pair)
    qty = quote_total = fee_q = base_fee = cash_fee = 0.0
    for tr in trades or []:
        q, price = float(tr["qty"]), float(tr["price"])
        qq = float(tr["quoteQty"]) if tr.get("quoteQty") is not None else q * price
        c = float(tr.get("commission", 0) or 0)
        qty += q
        quote_total += qq
        if tr.get("commissionAsset") == base:
            base_fee += c
            fee_q += c * price
        elif tr.get("commissionAsset") == quote:
            fee_q += c
            cash_fee += c
        else:                                   # comissão noutra moeda (ex.: BNB): estimada; não sai deste saldo
            fee_q += qq * G.FEE
    if qty <= 0:                                # sem detalhe das execuções: usa o resumo da ordem
        qty = float(resp.get("executedQty", 0) or 0)
        quote_total = float(resp.get("cummulativeQuoteQty", 0) or 0)
        if side == "buy":                       # a Binance cobra as compras na moeda comprada
            base_fee, cash_fee = qty * G.FEE, 0.0
            fee_q = base_fee * (quote_total / qty if qty else 0.0)
        else:
            fee_q = cash_fee = quote_total * G.FEE
    return (quote_total / qty if qty else 0.0), qty, fee_q, base_fee, cash_fee


class TestnetExecutor:
    __test__ = False   # o nome começa por "Test": não é uma classe de testes

    def __init__(self, trader):
        self.t = trader

    # ---------- utilidades ----------
    def _book(self, eng, o, resp, now, partial):
        trades = resp.get("fills") or self.t.order_trades(eng.pair, resp["orderId"])
        price, qty, fee, base_fee, cash_fee = summarize(eng.pair, resp, trades, o["side"])
        if qty <= 0:
            if o in eng.orders:
                eng.orders.remove(o)
            return
        eng.on_exchange_fill(o, now, price, qty, fee, base_fee, partial=partial, cash_fee=cash_fee)

    def _settle(self, eng, o, resp, now):
        """Aplica ao estado local o que a exchange diz sobre uma ordem nossa."""
        status = resp.get("status")
        if resp.get("orderId") is not None:
            o["oid"] = resp["orderId"]
        executed = float(resp.get("executedQty", 0) or 0)
        if status in (None, "NEW"):
            o["state"] = "open"
        elif status == "PARTIALLY_FILLED":
            o["state"], o["exec_qty"] = "partial", executed
        elif status == "FILLED":
            self._book(eng, o, resp, now, partial=False)
        elif executed > 0:                       # cancelada/expirada depois de executar uma parte
            self._book(eng, o, resp, now, partial=True)
        else:
            if o in eng.orders:
                eng.orders.remove(o)
            eng._event(now, "info", f"A Testnet fechou a ordem {o['cid']} ({status}) sem execução.")

    def _lookup(self, pair, cid):
        try:
            return self.t.get_order(pair, cid)
        except TraderError as exc:
            if exc.code in UNKNOWN_ORDER:
                return None
            raise

    # ---------- antes de o motor avançar ----------
    def reconcile(self, eng, now_ms):
        if eng.mode != "testnet":
            return
        if eng.orders or eng.s.get("cancel_queue"):
            self._reconcile_orders(eng, now_ms)
        if eng.status != "stopped":
            try:
                self._check_balance(eng, now_ms)
            except TraderError:
                pass                                # uma falha só na leitura do saldo não pára o passo

    def _reconcile_orders(self, eng, now_ms):
        queue = eng.s.get("cancel_queue") or []
        pair = eng.pair
        live = {r["clientOrderId"]: r for r in self.t.open_orders(pair)}
        missing = 0
        for o in list(eng.orders):
            cid = o.get("cid")
            if not cid:
                continue
            if cid in live:
                self._settle(eng, o, live[cid], now_ms)
                continue
            found = self._lookup(pair, cid)
            if found is None:
                if o["state"] != "sending":      # tínhamo-la como aberta e a exchange já não a conhece
                    missing += 1
                continue                         # 'sending' que nunca chegou: o flush envia
            self._settle(eng, o, found, now_ms)
        if missing:
            eng.testnet_reset(now_ms, f"{missing} ordem(ns) que estavam abertas desapareceram da Testnet")
            return
        known = {o["cid"] for o in eng.orders if o.get("cid")} | {q["cid"] for q in queue}
        prefix = eng.cid_prefix()
        for cid in live:                         # ordens nossas que o estado local não conhece: nunca as deixamos
            if cid.startswith(prefix) and cid not in known:
                eng._event(now_ms, "guard", f"Ordem {cid} desconhecida na Testnet: cancelada.")
                try:
                    resp = self.t.cancel_order(pair, cid)
                except TraderError as exc:
                    if exc.code not in UNKNOWN_ORDER:
                        raise
                    continue
                if float(resp.get("executedQty", 0) or 0) > 0:      # já executou uma parte: conta no saldo
                    lo = live[cid]
                    stray = {"slot": -2, "side": lo["side"].lower(), "price": float(lo["price"]),
                             "qty": float(lo["origQty"]), "gen": -1}
                    self._book(eng, stray, {**resp, "orderId": resp.get("orderId", lo.get("orderId"))}, now_ms, True)

    def _check_balance(self, eng, now_ms):
        """Compara a moeda da Testnet com a do bot: bastante menos = reset (ou mexeram na conta): pára; pouco = aviso."""
        base_qty = eng.s.get("base") or 0.0
        if now_ms - eng.s.get("balance_check_ts", 0) < BALANCE_CHECK_EVERY_MS or not base_qty:
            return
        eng.s["balance_check_ts"] = now_ms
        base, _ = _assets(eng.pair)
        have = next((float(b["free"]) + float(b["locked"]) for b in self.t.account() if b["asset"] == base), 0.0)
        tolerance = 2 * (eng.rules.get("step") or 0)
        if have + tolerance >= base_qty:
            return
        close = eng.s.get("last_close") or 0.0
        if have < RESET_BALANCE_RATIO * base_qty and base_qty * close >= eng.rules["min_notional"]:
            eng.testnet_reset(now_ms, f"o saldo de {base} na Testnet ({have:g}) é muito menor do que o do bot "
                                      f"({base_qty:g})")
        else:
            eng._event(now_ms, "info", f"O saldo de {base} na Testnet ({have:g}) é menor do que o bot julga ter "
                                       f"({base_qty:g}). Confirma se há outro bot ou uso manual na mesma conta.")

    # ---------- depois de o estado estar gravado ----------
    def flush(self, eng, now_ms):
        if eng.mode != "testnet":
            return
        queue = list(eng.s.get("cancel_queue") or [])
        for i, item in enumerate(queue):
            try:
                self._cancel_one(eng, item, now_ms)
            except TraderError:
                eng.s["cancel_queue"] = queue[i:]         # o que falhou (e o resto) fica para o passo seguinte
                raise
        eng.s["cancel_queue"] = []
        for _ in range(6):                                # uma execução pode gerar novas ordens (ex.: as vendas)
            pending = [x for x in eng.orders if x.get("state") == "sending"]
            if not pending:
                break
            for o in pending:
                self._send(eng, o, now_ms)

    def _cancel_one(self, eng, item, now):
        if item["slot"] < 0:                              # ordens ao mercado não se cancelam
            return
        pair = eng.pair
        try:
            resp = self.t.cancel_order(pair, item["cid"])
        except TraderError as exc:
            if exc.code not in UNKNOWN_ORDER:
                raise
            resp = self._lookup(pair, item["cid"])        # já não estava aberta: pode ter sido executada
            if resp is None:
                return                                    # nunca chegou à exchange
        executed = float(resp.get("executedQty", 0) or 0)
        if executed > 0:
            order = {k: item[k] for k in ("slot", "side", "price", "qty")}
            order["active_from"] = now
            order["gen"] = item.get("gen")                # a que grelha pertencia (a recentragem renumera os degraus)
            self._book(eng, order, {**resp, "orderId": resp.get("orderId", item.get("oid"))}, now,
                       partial=resp.get("status") != "FILLED")

    def _send(self, eng, o, now, retry=True):
        pair, rules = eng.pair, eng.rules
        found = self._lookup(pair, o["cid"])              # um envio anterior pode ter chegado sem termos registado
        if found is not None:
            self._settle(eng, o, found, now)
            return
        closing = o["slot"] < 0 and o["side"] == "sell"
        if closing:                                       # fechar a posição: vende o que o bot tem agora, tudo
            o["qty"] = G.round_down(eng.s["base"], rules.get("step"))
            if o["qty"] <= 0:
                eng.orders.remove(o)
                return
        n_open = sum(1 for x in eng.orders if x.get("state") in ("open", "partial"))
        try:
            G.validate_order(o, rules, eng.mode, n_open)
        except G.OrderRefused as exc:
            eng.fail_order(o, now, str(exc), stop=o["slot"] == -1 and o["side"] == "buy")
            return
        try:
            if o.get("type") == "market":
                resp = self.t.place_market(pair, o["side"], o["qty"], o["cid"], rules.get("step"))
            else:
                resp = self.t.place_limit(pair, o["side"], o["qty"], o["price"], o["cid"],
                                          rules.get("tick"), rules.get("step"))
        except TraderError as exc:
            if exc.code in (-1013, -1111):
                eng.fail_order(o, now, str(exc), stop=o["slot"] == -1 and o["side"] == "buy")
            elif exc.code == DUPLICATE:                    # ordem repetida OU saldo insuficiente: o id decide
                found = self._lookup(pair, o["cid"])
                if found is not None:
                    self._settle(eng, o, found, now)
                elif closing and retry:                    # a comissão em moeda deixa menos do que o bot julga: vende o livre
                    free = self.t.free_balance(_assets(pair)[0])
                    qty = G.round_down(min(o["qty"], free), rules.get("step"))
                    if 0 < qty < o["qty"]:
                        o["qty"] = qty
                        self._send(eng, o, now, retry=False)
                    else:
                        eng.fail_order(o, now, str(exc), stop=False)
                else:
                    eng.fail_order(o, now, str(exc), stop=o["slot"] == -1 and o["side"] == "buy")
            else:
                raise                                      # sem ligação, limite de pedidos, chaves
            return
        self._settle(eng, o, resp, now)
