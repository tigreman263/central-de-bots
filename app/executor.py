"""Executor da Testnet: traduz as intenções do motor em ordens reais e reconcilia o estado com a exchange.

O motor (engine.py) decide e nunca fala com a Binance. Aqui a exchange é a fonte da verdade:
- `reconcile` corre ANTES de o motor avançar: lê as ordens abertas e as execuções e ajusta o estado local;
- `flush` corre DEPOIS de o estado ter sido gravado: cancela o que o motor retirou e envia o que ele decidiu.
Cada ordem leva um id único (bot+degrau+lado+contador) gravado ANTES do envio. Se o processo morrer a meio, no
arranque seguinte procura-se o id na exchange: se já existe, adota-se; se não, envia-se. Nunca há duplicados.
Só comunica através de `trader.Trader` (o único módulo com ordens) e todas as ordens passam por `grid.validate_order`.

Contabilidade das execuções: cada trade da exchange tem um id e só é contabilizado uma vez (registo em
`bot_trade_registry`). O dinheiro, a moeda e as comissões contam-se trade a trade, ao ritmo a que a exchange os mostra;
o degrau só muda quando a ordem termina. Se a soma dos trades não bate com o `executedQty`, a ordem fica em
RECONCILIATION_PENDING (nada se disfarça) e tenta-se outra vez a cada passo.

Paragem (estado STOPPING): ver `_stop_step`. Nunca se declara STOPPED sem confirmar na exchange: sem ordens abertas do
bot, sem ordens por enviar ou reconciliar, e a posição do bot fechada (ou só pó que não dá para vender). Uma posição que
nenhum bot explica (UNKNOWN_POSITION) nunca se vende. Antes de cada POST de uma ordem de estratégia lê-se outra vez o
pedido de paragem (`stop_check`), e outra vez depois do POST: se a paragem apareceu entretanto, a ordem já enviada é
tratada como ORDER_IN_FLIGHT_DURING_STOP e entra logo no cancelamento e na reconciliação.

Recuperação (estado RECOVERING): quando o estado local e a exchange divergem (ordem que desapareceu, ordens do bot sem
dono local, saldo que não bate), o bot deixa de criar ordens, lê tudo da exchange, classifica as diferenças, contabiliza
o que faltava e só então volta a trabalhar. Nunca se apaga histórico financeiro.
"""
from . import grid as G
from .engine import PAUSED, PENDING, RECOVERING, RUNNING, STOPPED, STOPPING, cid_slot, parse_cid
from .trader import DUPLICATE, UNKNOWN_ORDER, TraderError

BALANCE_CHECK_EVERY_MS = 3_600_000
RESET_BALANCE_RATIO = 0.5      # a Testnet tem menos de metade da moeda que o bot julga ter: foi reposta (ou mexeram-lhe)
RECON_ADJUST_AFTER_MS = 6 * 3_600_000   # trades que não aparecem há 6 h: ajuste EXPLÍCITO (marcado e com alerta)
STOP_RETRY_CLOSE_MS = 60_000            # depois de 3 tentativas falhadas de fechar a posição, uma por minuto

# classificação do que se encontra ao comparar o estado local com a exchange
KNOWN_MATCH = "KNOWN_MATCH"                      # ordem nossa, igual dos dois lados
KNOWN_MISSING = "KNOWN_MISSING"                  # ordem que o bot julga aberta e a exchange já não conhece
UNKNOWN_BOT_ORDER = "UNKNOWN_BOT_ORDER"          # ordem aberta que é deste bot (id) mas o estado local não tem
FILLED_NOT_BOOKED = "FILLED_NOT_BOOKED"          # executada na exchange, ainda não contabilizada
CANCELLED_NOT_BOOKED = "CANCELLED_NOT_BOOKED"    # cancelada depois de executar uma parte que não estava contabilizada
UNKNOWN_POSITION = "UNKNOWN_POSITION"            # moeda na conta que nenhum bot (nem execução) explica
RECONCILIATION_PENDING = "settling"              # estado da ordem: terminou mas os trades ainda não batem com o executedQty


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


def trade_rows(trades):
    """Execuções da Binance (myTrades ou `fills` de uma ordem) num formato único, sempre com um id de trade.

    A Binance dá sempre id (`id` em myTrades, `tradeId` em `fills`); sem ele, cria-se um id estável a partir do conteúdo.
    """
    rows, dup = [], {}
    for tr in trades or []:
        q, price = float(tr["qty"]), float(tr["price"])
        tid = tr.get("id", tr.get("tradeId"))
        if tid is None:
            stem = f"x{tr.get('time', 0)}:{q:.10g}:{price:.10g}"
            dup[stem] = dup.get(stem, 0) + 1
            tid = f"{stem}:{dup[stem]}"
        rows.append({"id": str(tid), "qty": q, "price": price,
                     "quoteQty": float(tr["quoteQty"]) if tr.get("quoteQty") is not None else q * price,
                     "commission": float(tr.get("commission", 0) or 0), "commissionAsset": tr.get("commissionAsset"),
                     "time": tr.get("time")})
    return rows


def aggregate(pair, rows):
    """(quantidade, valor em USDT, comissão em USDT, comissão em moeda, comissão saída do saldo em USDT) de várias execuções."""
    if not rows:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    _, qty, fee, base_fee, cash_fee = summarize(pair, {}, rows)
    return qty, sum(r["quoteQty"] for r in rows), fee, base_fee, cash_fee


class TestnetExecutor:
    __test__ = False   # o nome começa por "Test": não é uma classe de testes

    def __init__(self, trader, stop_check=None):
        """`stop_check(eng)`: o motivo da paragem pedida (PARAR TUDO ou comando stop) lido AGORA, ou None."""
        self.t = trader
        self.stop_check = stop_check

    @staticmethod
    def _tol(eng):
        return max(1e-9, (eng.rules.get("step") or 0.0) * 1e-3)

    # ---------- contabilidade das execuções ----------
    def _fetch_trades(self, eng, o, resp, executed):
        """Trades da ordem: os que vêm na resposta e, se não chegarem, os do myTrades. Nada se assume completo."""
        cid = o["cid"]
        rows = trade_rows(resp.get("fills"))
        known = eng.seen.get(cid, {})
        have = eng.booked(cid) + sum(r["qty"] for r in rows if r["id"] not in known)
        if have < executed - self._tol(eng):
            rows += trade_rows(self.t.order_trades(eng.pair, resp.get("orderId", o.get("oid"))))
        return rows

    def _book(self, eng, o, resp, now, terminal):
        """Contabiliza o que a exchange mostra de uma ordem (só trades novos) e, se terminou, fecha-a.

        Invariantes: contabilizado <= executedQty; uma ordem terminada tem contabilizado == executedQty ou fica em
        RECONCILIATION_PENDING (com evento) e volta a tentar-se.
        """
        cid = o["cid"]
        status = resp.get("status")
        executed = float(resp.get("executedQty", 0) or 0)
        tol = self._tol(eng)
        fresh = False
        if executed > eng.booked(cid) + tol:
            rows = self._fetch_trades(eng, o, resp, executed)
            new = eng.register_trades(cid, resp.get("orderId", o.get("oid")), o["side"], rows, now)
            if new:
                fresh = True
                eng.apply_trades(o, now, *aggregate(eng.pair, new))
        o["exec_qty"] = executed
        if not terminal:
            o["state"] = "partial"
            return
        booked = eng.booked(cid)
        if booked > executed + tol:
            eng._event(now, "guard", f"Ordem {cid}: contabilizei {booked:g} mas a Testnet diz {executed:g} executados. "
                                     "A contabilidade precisa de revisão.")
        if executed - booked > tol:
            if not self._mark_pending(eng, o, resp, now, executed, booked):
                return
        o.pop("pending_since", None)
        if fresh:
            eng.findings.append((FILLED_NOT_BOOKED if status == "FILLED" else CANCELLED_NOT_BOOKED, cid))
        eng.finish_order(o, now, status != "FILLED", *aggregate(eng.pair, list(eng.seen.get(cid, {}).values())))

    def _mark_pending(self, eng, o, resp, now, executed, booked):
        """Trades em falta: a ordem fica à espera (RECONCILIATION_PENDING). Depois de muito tempo, ajusta-se, às claras."""
        cid = o["cid"]
        if o.get("state") != RECONCILIATION_PENDING:
            o["state"] = RECONCILIATION_PENDING
            o["pending_since"] = now
            eng._event(now, "recon", f"Ordem {cid}: a Testnet diz {executed:g} executados mas só há trades para {booked:g}. "
                                     "A aguardar os trades em falta (RECONCILIATION_PENDING).")
            eng.findings.append((RECONCILIATION_PENDING, cid))
        if o not in eng.orders:                          # ordem que só conhecíamos por cancelamento ou por procura
            taken = {(x["slot"], x["side"]) for x in eng.orders}
            slot = -1000
            while (slot, o["side"]) in taken:
                slot -= 1
            o.update(slot=slot, gen=-1, active_from=now)   # tratada como pó, sem degrau
            eng.orders.append(o)
        if now - o.get("pending_since", now) < RECON_ADJUST_AFTER_MS:
            return False
        missing = executed - booked
        quote = float(resp.get("cummulativeQuoteQty", 0) or 0) - sum(r["quoteQty"] for r in eng.seen.get(cid, {}).values())
        if quote <= 0:
            quote = missing * float(o.get("price") or 0.0)
        row = {"id": f"adj:{cid}", "qty": missing, "price": quote / missing, "quoteQty": quote, "commission": 0.0,
               "commissionAsset": None, "time": now}
        new = eng.register_trades(cid, resp.get("orderId", o.get("oid")), o["side"], [row], now, status="adjusted")
        if new:
            eng.apply_trades(o, now, *aggregate(eng.pair, new))
        eng._event(now, "guard", f"Ordem {cid}: os trades em falta ({missing:g}) não apareceram em "
                                 f"{RECON_ADJUST_AFTER_MS // 3_600_000} h. Ajuste explícito pelo resumo da ordem, sem comissão.")
        return True

    def _settle(self, eng, o, resp, now):
        """Aplica ao estado local o que a exchange diz sobre uma ordem nossa."""
        status = resp.get("status")
        if resp.get("orderId") is not None:
            o["oid"] = resp["orderId"]
        executed = float(resp.get("executedQty", 0) or 0)
        if status in (None, "NEW"):
            o["state"] = "open"
        elif status == "PARTIALLY_FILLED":
            self._book(eng, o, resp, now, terminal=False)
        elif executed > 0 or eng.booked(o["cid"]) > 0:   # terminou (cheia, cancelada ou expirada) depois de executar algo
            self._book(eng, o, resp, now, terminal=True)
        else:
            if o in eng.orders:
                eng.orders.remove(o)
            eng._event(now, "info", f"A Testnet fechou a ordem {o['cid']} ({status}) sem execução.")

    def _book_stray(self, eng, cid, meta, resp, now):
        """Uma ordem do bot que o estado local não tinha (ou já tinha largado): conta o que executou.

        Se ainda corresponde a um degrau da grelha atual (degrau e lado no id, preço igual, degrau no estado certo),
        fecha esse degrau como qualquer execução; senão fica como pó sem degrau (órfã).
        """
        merged = {**meta, **resp}
        if float(merged.get("executedQty", 0) or 0) <= 0 and eng.booked(cid) <= 0:
            return
        stray = {"slot": -2, "side": str(meta["side"]).lower(), "price": float(meta["price"]),
                 "qty": float(meta["origQty"]), "gen": -1, "cid": cid, "oid": meta.get("orderId"), "state": "partial"}
        where = cid_slot(cid)
        if where and where[1] in ("b", "s") and eng.fits_grid(where[0], stray["side"], stray["price"]):
            stray["slot"], stray["gen"] = where[0], None
        self._book(eng, stray, merged, now, terminal=True)

    def _lookup(self, pair, cid):
        try:
            return self.t.get_order(pair, cid)
        except TraderError as exc:
            if exc.code in UNKNOWN_ORDER:
                return None
            raise

    # ---------- antes de o motor avançar ----------
    def reconcile(self, eng, now_ms, claimed=None):
        """Compara o estado local com a exchange, corre SEMPRE (mesmo sem ordens locais) e recupera se for preciso.

        `claimed`: (moeda, USDT) que os outros bots da mesma conta dizem ter, para o saldo da conta ser comparado com
        o total de todos e não só com este bot.
        """
        if eng.mode != "testnet" or (eng.status == PENDING and not eng.orders):
            return
        eng.findings = []
        live = {r["clientOrderId"]: r for r in self.t.open_orders(eng.pair)}
        for cid in live:
            eng.note_cid(cid)
        local = {o["cid"] for o in eng.orders if o.get("cid")} | {q["cid"] for q in eng.s.get("cancel_queue") or []}
        missing = self._match_local(eng, live, now_ms)
        unknown = [cid for cid in live if eng.owns(cid) and cid not in local]
        reasons = []
        if missing:
            reasons.append(f"{len(missing)} ordem(ns) que estavam abertas desapareceram da Testnet")
        if unknown:
            reasons.append(f"{len(unknown)} ordem(ns) deste bot na Testnet sem registo local")
        active = eng.status in (RUNNING, PAUSED)
        if not reasons and active:
            try:
                why = self._check_balance(eng, now_ms, claimed, live)
            except TraderError:
                why = None                              # uma falha só na leitura do saldo não pára o passo
            if why:
                reasons.append(why)
        if reasons and active:
            eng.enter_recovery(now_ms, reasons)
        if eng.status in (RECOVERING, STOPPING) or (reasons and eng.status == STOPPED):
            self._recover(eng, now_ms, live, missing, unknown, claimed)

    def _match_local(self, eng, live, now_ms):
        """Cada ordem local contra a exchange. Devolve as que a Testnet já não conhece (KNOWN_MISSING)."""
        pair, missing = eng.pair, []
        for o in list(eng.orders):
            cid = o.get("cid")
            if not cid:
                continue
            if cid in live:
                eng.findings.append((KNOWN_MATCH, cid))
                self._settle(eng, o, live[cid], now_ms)
                continue
            found = self._lookup(pair, cid)
            if found is None:
                if o["state"] != "sending":      # tínhamo-la como aberta e a exchange já não a conhece
                    missing.append(o)
                    eng.findings.append((KNOWN_MISSING, cid))
                continue                         # 'sending' que nunca chegou: o flush envia
            self._settle(eng, o, found, now_ms)
        return missing

    # ---------- recuperação ----------
    def _recover(self, eng, now_ms, live, missing, unknown, claimed):
        pair = eng.pair
        counts = {}

        def count(kind):
            counts[kind] = counts.get(kind, 0) + 1

        # a) ordens locais que a Testnet já não conhece: sai o registo da ordem (nunca a contabilidade já feita)
        for o in missing:
            count(KNOWN_MISSING)
            if eng.booked(o["cid"]) > 0:                 # já tinha execuções contadas: fecha o degrau com o que há
                eng.finish_order(o, now_ms, True, *aggregate(pair, list(eng.seen.get(o["cid"], {}).values())))
            elif o in eng.orders:
                eng.orders.remove(o)
            eng._event(now_ms, "recon", f"{KNOWN_MISSING}: a ordem {o['cid']} ({o['side']}, degrau {o['slot']}) já não existe na Testnet.")
        # b) ordens do bot na Testnet que o estado local não tem: cancelam-se e conta-se o que já executaram
        for cid in unknown:
            count(UNKNOWN_BOT_ORDER)
            try:
                resp = self.t.cancel_order(pair, cid)
            except TraderError as exc:
                if exc.code not in UNKNOWN_ORDER:
                    raise
                continue                                  # terminou entretanto: a passagem seguinte apanha-a
            self._book_stray(eng, cid, live[cid], resp, now_ms)
            eng._event(now_ms, "recon", f"{UNKNOWN_BOT_ORDER}: a ordem {cid} era deste bot mas o estado local não a tinha; cancelada.")
        # c) ordens do bot já terminadas que nunca foram contabilizadas (crash, cópia de segurança, reset local)
        for kind, r in self._unbooked(eng, live):
            count(kind)
            self._book_stray(eng, r["clientOrderId"], r, r, now_ms)
            eng._event(now_ms, "recon", f"{kind}: a ordem {r['clientOrderId']} executou {r.get('executedQty')} e não estava "
                                        "contabilizada; contada agora.")
        # d) saldos, nos dois sentidos
        snap = self._snapshot(eng, now_ms, claimed)
        base, mine, have = _assets(pair)[0], snap["mine"], snap["have"]
        close = eng.s.get("last_close") or 0.0
        if snap["state"] == "local_gt_exchange" and have < RESET_BALANCE_RATIO * mine \
                and mine * close >= eng.rules["min_notional"]:
            why = f"o saldo de {base} na Testnet ({have:g}) é muito menor do que o do bot ({mine:g})"
            eng.testnet_reset(now_ms, (f"{len(missing)} ordem(ns) desapareceram e " if missing else "") + why)
            return
        if snap["state"] == "local_gt_exchange":
            eng._event(now_ms, "info", f"O saldo de {base} na Testnet ({have:g}) é menor do que o bot julga ter "
                                       f"({mine:g}). Confirma se há outro bot ou uso manual na mesma conta.")
        elif snap["state"] == "local_lt_exchange":
            count(UNKNOWN_POSITION)
            eng._event(now_ms, "guard" if eng.status == STOPPING else "recon", f"{UNKNOWN_POSITION}: a conta tem {snap['excess']:g} {base} a mais do que todos os "
                                        "bots dizem ter. Fica como está: não é vendida nem adotada.")
        eng.s["excess_seen"] = max(snap["excess"], 0.0)
        if snap["usdt_state"] == "local_gt_exchange":
            eng._event(now_ms, "info", f"O saldo de USDT na Testnet ({snap['usdt']:.2f}) é menor do que os bots dizem ter "
                                       f"({snap['cash']:.2f}).")
        summary = ", ".join(f"{k}={n}" for k, n in sorted(counts.items())) or "sem diferenças por corrigir"
        if eng.status == RECOVERING:
            eng.finish_recovery(now_ms, summary)
        elif counts:
            eng._event(now_ms, "recon", f"Reconciliação: {summary}.")
        if eng.status == STOPPING:
            self._stop_step(eng, now_ms, live, unknown)

    def _stop_step(self, eng, now_ms, live, unknown):
        """Depois de reconciliar: ou ainda há trabalho (esperar), ou fecha-se a posição do bot, ou confirma-se STOPPED.

        Só se dá por parado com a exchange lida e nada por fazer. A quantidade a vender é a do estado real reconciliado:
        nunca mais do que o bot tem, nem mais do que existe na conta (menos o que os outros bots dizem ter).
        """
        s, st = eng.s, eng.s["stop"]
        cancelled = set(unknown)
        busy = s.get("cancel_queue") or [o for o in eng.orders if o.get("state") in ("sending", "open", "partial",
                                                                                     RECONCILIATION_PENDING)] \
            or [c for c in live if eng.owns(c) and c not in cancelled]
        if busy:
            return
        snap = s.get("balance") or {}
        rules, close = eng.rules, s.get("last_close") or 0.0
        avail = max(0.0, snap.get("have", 0.0) - snap.get("others", 0.0))
        qty = G.round_down(min(s.get("base") or 0.0, avail), rules.get("step"))
        if st.get("sell", True) and qty > 0 and qty >= (rules.get("min_qty") or 0.0) and qty * close >= rules["min_notional"]:
            if st["attempts"] >= 3 and now_ms - st.get("last_attempt", 0) < STOP_RETRY_CLOSE_MS:
                return                                    # já falhou algumas vezes: tenta de novo daqui a pouco
            st["attempts"] += 1
            st["last_attempt"] = now_ms
            eng.orders.append(eng._new_order(-1, "sell", close, qty, now_ms, kind="market", tag="l"))   # ordem de emergência
            return
        eng.finish_stop(now_ms)

    def _unbooked(self, eng, live):
        """Ordens deste bot já terminadas, sem dono local, com execuções por contabilizar: [(tipo, ordem da exchange)]."""
        local = {o["cid"] for o in eng.orders if o.get("cid")} | {q["cid"] for q in eng.s.get("cancel_queue") or []}
        since, tol, found = eng.s.get("registry_from", 0), self._tol(eng), []
        for r in self.t.all_orders(eng.pair):
            cid = r.get("clientOrderId", "")
            if not eng.owns(cid):
                continue
            eng.note_cid(cid)
            if cid in local or cid in live or r.get("status") in ("NEW", "PARTIALLY_FILLED"):
                continue
            if float(r.get("updateTime", 0) or 0) < since:
                continue                                  # anterior ao registo de trades: já contada pelo método antigo
            if float(r.get("executedQty", 0) or 0) <= eng.booked(cid) + tol:
                continue
            found.append((FILLED_NOT_BOOKED if r.get("status") == "FILLED" else CANCELLED_NOT_BOOKED, r))
        return sorted(found, key=lambda kr: parse_cid(kr[1]["clientOrderId"])[2])     # pela ordem em que foram criadas

    def _snapshot(self, eng, now_ms, claimed):
        """Saldos da conta contra o que os bots dizem ter, nos DOIS sentidos, para a moeda do par e para USDT."""
        base, quote = _assets(eng.pair)
        accounts = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in self.t.account()}
        have, usdt = accounts.get(base, 0.0), accounts.get(quote, 0.0)
        others_base, others_cash = claimed or (0.0, 0.0)
        mine = eng.s.get("base") or 0.0
        cash = (eng.s.get("quote") or 0.0) + (eng.s.get("reserve") or 0.0) + others_cash
        tol = max(2 * (eng.rules.get("step") or 0.0), 1e-9)
        pending = 0.0                                     # execuções que a exchange já mostra e ainda não contámos
        for o in eng.orders:
            if o.get("cid"):
                gap = max(0.0, float(o.get("exec_qty") or 0.0) - eng.booked(o["cid"]))
                pending += gap if o["side"] == "buy" else -gap
        expected = mine + others_base + max(pending, 0.0)
        state = "local_gt_exchange" if have + tol < mine + min(pending, 0.0) else (
            "local_lt_exchange" if have > expected + tol else "match")
        usdt_state = "local_gt_exchange" if usdt + 0.01 < cash else ("account_larger" if usdt > cash + 0.01 else "match")
        snap = {"ts": now_ms, "asset": base, "mine": mine, "others": others_base, "have": have, "pending": pending,
                "excess": have - expected,
                "tol": tol, "state": state, "usdt": usdt, "cash": cash, "usdt_state": usdt_state}
        eng.s["balance"] = snap
        eng.s["balance_check_ts"] = now_ms
        return snap

    def _check_balance(self, eng, now_ms, claimed=None, live=None):
        """Verificação horária. Devolve o motivo se for preciso recuperar (saldo muito menor ou moeda nova sem explicação)."""
        if now_ms - eng.s.get("balance_check_ts", 0) < BALANCE_CHECK_EVERY_MS:
            return None
        snap = self._snapshot(eng, now_ms, claimed)
        base, mine, have = snap["asset"], snap["mine"], snap["have"]
        if snap["usdt_state"] == "local_gt_exchange":     # só se avisa: o USDT nunca serve de gatilho de reset
            eng._event(now_ms, "info", f"O saldo de USDT na Testnet ({snap['usdt']:.2f}) é menor do que os bots dizem ter "
                                       f"({snap['cash']:.2f}).")
        if snap["state"] == "local_gt_exchange":
            close = eng.s.get("last_close") or 0.0
            if have < RESET_BALANCE_RATIO * mine and mine * close >= eng.rules["min_notional"]:
                return f"o saldo de {base} na Testnet ({have:g}) é muito menor do que o do bot ({mine:g})"
            eng._event(now_ms, "info", f"O saldo de {base} na Testnet ({have:g}) é menor do que o bot julga ter "
                                       f"({mine:g}). Confirma se há outro bot ou uso manual na mesma conta.")
            return None
        excess = max(snap["excess"], 0.0)
        seen = eng.s.get("excess_seen")
        if seen is None:                                  # primeira leitura: é a referência (a conta pode ter saldo próprio)
            eng.s["excess_seen"] = excess
            if live is not None and self._unbooked(eng, live):   # mas antes vê-se se há execuções nossas por contabilizar
                return "há ordens deste bot executadas na Testnet que não estão contabilizadas"
        elif abs(excess - seen) > snap["tol"]:
            return f"o saldo de {base} na Testnet ({have:g}) mudou sem explicação ({excess - seen:+g} face à referência)"
        return None

    # ---------- depois de o estado estar gravado ----------
    def flush(self, eng, now_ms):
        if eng.mode != "testnet":
            return
        for _ in range(6):                                # uma execução (ou uma paragem) pode gerar mais trabalho
            self._drain_cancels(eng, now_ms)
            if eng.status == RECOVERING:                  # cancelar pode; enviar ordens novas não, até a recuperação acabar
                return
            pending = [x for x in eng.orders if x.get("state") == "sending"]
            if not pending and not eng.s.get("cancel_queue"):
                break
            for o in pending:
                self._send(eng, o, now_ms)

    def _drain_cancels(self, eng, now_ms):
        queue = list(eng.s.get("cancel_queue") or [])
        for i, item in enumerate(queue):
            try:
                self._cancel_one(eng, item, now_ms)
            except TraderError:
                eng.s["cancel_queue"] = queue[i:]         # o que falhou (e o resto) fica para o passo seguinte
                raise
        eng.s["cancel_queue"] = []

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
                if eng.booked(item["cid"]) > 0:           # a exchange perdeu-a mas já contámos parte: fecha com isso
                    order = {k: item[k] for k in ("slot", "side", "price", "qty")}
                    order.update(cid=item["cid"], active_from=now, gen=item.get("gen"))
                    eng.finish_order(order, now, True, *aggregate(pair, list(eng.seen.get(item["cid"], {}).values())))
                return                                    # nunca chegou à exchange
        if float(resp.get("executedQty", 0) or 0) > 0 or eng.booked(item["cid"]) > 0:
            order = {k: item[k] for k in ("slot", "side", "price", "qty")}
            order["active_from"] = now
            order["cid"] = item["cid"]
            order["gen"] = item.get("gen")                # a que grelha pertencia (a recentragem renumera os degraus)
            self._book(eng, order, {**resp, "orderId": resp.get("orderId", item.get("oid"))}, now,
                       terminal=resp.get("status") not in ("NEW", "PARTIALLY_FILLED"))

    @staticmethod
    def _same_order(o, found, rules):
        """A ordem que a exchange tem com este id é mesmo a que íamos enviar? (cópias antigas repetiam ids)"""
        if str(found.get("side", o["side"])).lower() != o["side"]:
            return False
        is_market = str(found.get("type", "")).upper() == "MARKET"
        if is_market or o.get("type") == "market":
            return is_market == (o.get("type") == "market")
        if found.get("price") is None or found.get("origQty") is None:
            return True
        return abs(float(found["price"]) - o["price"]) <= max(rules.get("tick") or 0.0, 1e-9) and \
            abs(float(found["origQty"]) - o["qty"]) <= max(rules.get("step") or 0.0, 1e-9)

    def _stop_seen(self, eng, now):
        """Há uma paragem pedida que o motor ainda não tinha visto? Então entra já em STOPPING (e nada mais se cria)."""
        if eng.status == STOPPING:
            return True
        why = self.stop_check(eng) if self.stop_check else None
        if why:
            eng.request_stop(now, why)
        return bool(why)

    def _send(self, eng, o, now, retry=True):
        pair, rules = eng.pair, eng.rules
        closing = o["slot"] < 0 and o["side"] == "sell"   # a ordem de emergência que fecha a posição: essa pode sair
        if not closing and self._stop_seen(eng, now):     # 1.ª leitura: antes de preparar a ordem
            if o in eng.orders:                           # (a paragem já a tirou da lista, se acabou de ser pedida)
                eng._set_orders([x for x in eng.orders if x is not o])
            return
        found = self._lookup(pair, o["cid"])              # um envio anterior pode ter chegado sem termos registado
        if found is not None and not self._same_order(o, found, rules):
            eng._event(now, "guard", f"O id {o['cid']} já existe na Testnet com outra ordem (cópia antiga?): a nova ordem "
                                     "leva um id novo.")
            eng.rekey(o)
            found = None
        if found is not None:
            self._settle(eng, o, found, now)
            return
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
        if not closing and self._stop_seen(eng, now):     # 2.ª leitura: imediatamente antes do POST
            if o in eng.orders:
                eng._set_orders([x for x in eng.orders if x is not o])
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
        if not closing and eng.status != STOPPING and self.stop_check:     # 3.ª leitura: a paragem chegou durante o POST?
            why = self.stop_check(eng)
            if why:
                eng._event(now, "guard", f"ORDER_IN_FLIGHT_DURING_STOP: a ordem {o['cid']} saiu quando a paragem já estava "
                                         "pedida; vai ser cancelada e reconciliada.")
                eng.request_stop(now, why)
