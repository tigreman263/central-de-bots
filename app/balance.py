"""Verifica se o dinheiro em bots ativos ultrapassa a fatia "Trading" configurada (Configuração > divisão
reserva/trading/caixa). Nunca bloqueia nada nem move dinheiro: só avisa, para o utilizador decidir.
"""


def trading_usage(active_capital_usdt, snapshot, split_trading_pct):
    """Compara o capital comprometido em bots ativos com a fatia "Trading" do portefólio real.

    Nunca inventa um valor: sem portefólio real ligado (`snapshot` None ou sem total), o limite e o excesso
    ficam a None e `over` fica False — não há como avisar sem saber o valor real da carteira.
    """
    if not snapshot or not snapshot.get("total_usdt"):
        return {"active_usdt": active_capital_usdt, "limit_usdt": None, "over_usdt": None, "over": False}
    limit_usdt = snapshot["total_usdt"] * split_trading_pct / 100
    over_usdt = active_capital_usdt - limit_usdt
    return {"active_usdt": active_capital_usdt, "limit_usdt": limit_usdt,
            "over_usdt": over_usdt if over_usdt > 0 else 0.0, "over": over_usdt > 0}
