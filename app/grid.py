"""Grelha: matemática e guarda de risco. Só simulação. Todos os números são pontos de partida a validar."""
import math

FEE = 0.001            # comissão por execução (0,1%)
SLIP = 0.0005          # deslizamento fixo contra nós (0,05%)
FILL_MARGIN = 0.0005   # regra conservadora: o preço tem de passar um pouco além do nível

DEFAULTS = {
    "range_pct": 8.0,             # intervalo ±8% em redor do preço inicial
    "max_levels": 8,
    "cash_reserve_pct": 25.0,     # fora da grelha, nunca gasto
    "inventory_limit_pct": 65.0,  # máximo do capital em moeda
    "max_consecutive_buys": 3,    # depois disto, para de comprar até haver uma venda
    "stop_below_pct": 2.5,        # stop-loss abaixo do limite inferior
    "stop_minutes": 15,           # minutos seguidos com fecho abaixo do stop
    "upper_wait_minutes": 180,    # espera acima do limite superior antes de recentrar
    "max_recenter_per_day": 2,
    "drop_pause_pct": 4.0,        # pausa se cair mais disto numa hora
    "drop_window_minutes": 60,
    "daily_loss_pct": 3.0,        # limite diário de perda (% do capital do bot)
    "max_loss_trade_pct": 2.0,    # perda máxima por operação (% do capital do bot)
    "pause_drawdown_pct": 20.0,   # pausa se o capital do bot cair tanto
    "min_step_folga_pct": 0.3,    # folga mínima acima de 2x (comissão + deslizamento)
}


class GridRefused(Exception):
    """A guarda de risco recusou esta configuração (mensagem em português simples)."""


class OrderRefused(Exception):
    """A guarda de risco recusou uma ordem antes de sair (mensagem em português simples)."""


MAX_OPEN_ORDERS = 40   # muito acima dos degraus de um bot; barra bugs que inundem a exchange


def _on_grid(x, unit):
    if not unit:
        return True
    n = x / unit
    return abs(n - round(n)) < 1e-6


def validate_order(o, rules, mode, open_count=0):
    """Guarda única de ordens: nada sai para a exchange sem passar aqui. Só o modo testnet envia ordens."""
    if mode != "testnet":
        raise OrderRefused("Só os bots em modo Testnet podem enviar ordens (e só para a Testnet).")
    if o["side"] not in ("buy", "sell") or o.get("type", "limit") not in ("limit", "market"):
        raise OrderRefused("Tipo de ordem desconhecido.")
    qty, price = o["qty"], o["price"]
    if qty <= 0 or price <= 0:
        raise OrderRefused("Preço e quantidade têm de ser maiores que zero.")
    if open_count >= MAX_OPEN_ORDERS:
        raise OrderRefused(f"Demasiadas ordens abertas ({open_count}).")
    if not _on_grid(qty, rules.get("step")):
        raise OrderRefused(f"Quantidade {qty:g} fora do passo do par ({rules.get('step'):g}).")
    if qty < (rules.get("min_qty") or 0) or (rules.get("max_qty") and qty > rules["max_qty"]):
        raise OrderRefused(f"Quantidade {qty:g} fora dos limites do par.")
    if o.get("type", "limit") == "limit" and not _on_grid(price, rules.get("tick")):
        raise OrderRefused(f"Preço {price:g} fora do passo de preço do par ({rules.get('tick'):g}).")
    if qty * price < rules["min_notional"]:
        raise OrderRefused(f"Ordem de {qty * price:.2f} USDT abaixo do mínimo da Binance ({rules['min_notional']:g}).")


def round_down(x, step):
    if not step:
        return x
    return math.floor(x / step + 1e-9) * step


def round_price(x, tick):
    if not tick:
        return x
    return round(round(x / tick) * tick, 12)


def cost_pct():
    return (2 * FEE + 2 * SLIP) * 100


def build_grid(center, capital, rules, params):
    """Devolve a grelha ou levanta GridRefused. `rules`: {tick, step, min_notional}."""
    p = {**DEFAULTS, **params}
    budget = capital * (1 - p["cash_reserve_pct"] / 100)
    min_notional = rules["min_notional"] * 1.05
    lower = center * (1 - p["range_pct"] / 100)
    upper = center * (1 + p["range_pct"] / 100)
    stop_price = lower * (1 - p["stop_below_pct"] / 100)
    required = cost_pct() + p["min_step_folga_pct"]

    n = min(int(p["max_levels"]), int(budget // min_notional))
    while n >= 3:
        prices = [round_price(lower + i * (upper - lower) / n, rules.get("tick")) for i in range(n + 1)]
        step_pct = min((prices[i + 1] / prices[i] - 1) * 100 for i in range(n))
        if step_pct >= required:
            break
        n -= 1
    else:
        n = 0
    if n < 3:
        raise GridRefused(
            f"Não dá para montar uma grelha segura com {capital:.2f} USDT: são precisos pelo menos 3 degraus com "
            f"ordens acima do mínimo da Binance ({rules['min_notional']:g} USDT) e lucro por degrau acima de "
            f"{required:.2f}% (comissões e deslizamento com folga).")

    slots = []
    for i in range(n):
        quote = budget / n
        loss_share = 1 - stop_price / prices[i]                       # perda se comprar aqui e o stop disparar
        quote = min(quote, p["max_loss_trade_pct"] / 100 * capital / loss_share) if loss_share > 0 else quote
        qty = round_down(quote / prices[i], rules.get("step"))
        if qty * prices[i] < rules["min_notional"]:
            raise GridRefused(
                f"A perda máxima por operação ({p['max_loss_trade_pct']:g}% do capital) obriga a ordens abaixo do "
                f"mínimo da Binance. Aumenta o capital do bot ou reduz o intervalo.")
        worst = qty * prices[i] * loss_share / capital * 100
        slots.append({"i": i, "qty": qty, "holding": False, "buy_cost": 0.0, "worst_loss_pct": worst})
    return {"prices": prices, "slots": slots, "lower": lower, "upper": upper, "stop": stop_price,
            "step_pct": step_pct, "levels": n, "center": center, "budget": budget}
