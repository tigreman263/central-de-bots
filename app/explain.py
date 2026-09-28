"""Explicações em linguagem simples para a central de bots. Só texto: nada aqui decide ou envia ordens."""

GRID = {
    "title": "Como funciona um bot de grelha",
    "paras": [
        "Escolhes um par (por exemplo ETH/USDT). O bot define um intervalo de preço à volta do preço de hoje "
        "(por defeito ±8%) e divide-o em degraus, como os degraus de uma escada.",
        "Coloca ordens de compra nos degraus abaixo do preço e ordens de venda nos degraus acima. Quando o preço "
        "desce e uma compra executa, o bot coloca logo uma venda um degrau acima. Quando essa venda executa, "
        "ganha a diferença entre os dois degraus, menos as comissões. E repete, sem parar.",
        "Exemplo: compra a 98 e vende a 100. Ganha cerca de 2% dessa fatia, menos cerca de 0,3% de custos "
        "(comissões e deslizamento).",
    ],
    "wins": "Ganha quando o preço anda de um lado para o outro dentro do intervalo (mercado lateral).",
    "loses": "Perde quando o preço cai muito abaixo do intervalo (fica com moedas compradas mais caras do que valem). "
             "Quando sobe muito acima, vende tudo cedo e deixa de ganhar.",
    "sim": "É simulação: usa preços reais da Binance mas dinheiro virtual. Nunca toca na tua conta.",
}

STATES = {
    "running": ("A trabalhar", "Tem ordens ativas e está a comprar e a vender."),
    "paused": ("Em pausa", "Não faz compras novas; as vendas que já estavam abertas mantêm-se. Carrega em Retomar quando quiseres."),
    "stopping": ("A parar", "A paragem foi pedida. Cancela as ordens, contabiliza o que já executou e fecha a posição. Só fica Parado depois de a Testnet o confirmar."),
    "recovering": ("A recuperar", "O estado do bot e a Testnet divergiram. Não cria ordens novas: lê tudo da Testnet, contabiliza o que faltava e só depois volta a trabalhar."),
    "pending": ("A arrancar", "Está criado e aprovado. O corredor monta a grelha no próximo minuto."),
    "stopped": ("Parado", "Fechou a posição e cancelou tudo. Não volta a arrancar sozinho: carrega em Ativar neste bot quando quiseres."),
}

STATS = {
    "net_profit": "Quanto o capital do bot mudou desde o início, já depois de comissões e contando as moedas que ainda tem. Pode ser negativo.",
    "cycles": "Um ciclo é uma compra seguida da venda correspondente. Cada ciclo é o lucro (ou a perda) de um degrau.",
    "fees": "Comissão simulada de 0,1% em cada compra e venda. Come uma parte de cada ganho.",
    "worst_loss": "A maior perda num único degrau, em % do capital do bot. O limite vem da Configuração (por defeito 2%).",
    "day_loss": "Quanto o capital do bot desceu hoje. Se chegar ao limite diário, o bot pára de comprar sozinho.",
    "running": "Horas seguidas desde que arrancou. A meta da v0.2 é 168 horas (7 dias) sem intervenção.",
    "inventory": "Parte do capital que está em moeda em vez de USDT. O limite é 65%.",
    "orders": "Ordens de compra e venda simuladas à espera de o preço lá chegar.",
    "capital": "Dinheiro virtual do bot. Não sai da tua conta e não vende nada que tenhas.",
    "equity_chart": "O capital total do bot (USDT em caixa mais o valor da moeda) ao longo do tempo, uma leitura por hora.",
}

PROTECTIONS = [
    ("cash_reserve_pct", "Reserva de caixa", "Uma parte do capital fica sempre em USDT, fora da grelha, como colchão. Nunca é gasta."),
    ("max_loss_trade_pct", "Perda máxima por operação", "O tamanho de cada degrau é calculado para que, se o stop disparar, a perda nesse degrau fique abaixo deste limite."),
    ("stop", "Stop-loss", "Se o preço fechar abaixo deste nível vários minutos seguidos, o bot fecha tudo. Limita a perda numa queda forte, mas pode disparar num pavio e fixar uma perda que recuperaria."),
    ("inventory_limit_pct", "Limite de inventário", "Impede o bot de ficar com quase todo o capital em moeda. Mesmo numa queda, sobra sempre USDT."),
    ("max_consecutive_buys", "Compras seguidas", "Várias compras sem nenhuma venda pelo meio são sinal de tendência de queda. O bot para de comprar até haver uma venda."),
    ("drop_pause_pct", "Queda rápida", "Se o preço cair muito numa hora, o bot pausa para não comprar uma queda a meio. Pode pausar em falso num movimento rápido normal."),
    ("daily_loss_pct", "Limite diário", "Se o capital do bot descer este valor num dia, pára de comprar. Evita que um mau dia se torne num desastre."),
    ("pause_drawdown_pct", "Perda total", "Se o capital do bot cair tanto desde o início, pausa. Serve para reavaliares a estratégia."),
    ("upper_wait_minutes", "Acima do intervalo", "Se o preço sobe acima do intervalo e lá fica, o bot já vendeu tudo. Depois de esperar, recentra a grelha, no máximo algumas vezes por dia, para não perseguir o preço."),
]

FILTERS = [
    "Volume: só pares com muito dinheiro a ser negociado em 24 horas (mais de 20 milhões de USDT), para conseguires comprar e vender sem mexer no preço.",
    "Spread: só pares onde a diferença entre comprar e vender é minúscula (até 0,05%), porque essa diferença é um custo.",
    "Movimento: só pares que se mexeram entre 2% e 8% nas últimas 24 horas. Parado, a grelha não faz nada; muito agitado, sai do intervalo.",
    "Fora da lista: memecoins, moedas alavancadas e stablecoins, conforme as categorias que evitas na Configuração.",
]

GRID_TERMS = [
    ("Degraus", "Os níveis de preço onde o bot coloca ordens."),
    ("Degrau mínimo", "A distância mais pequena entre dois degraus, em %. Tem de cobrir as comissões e o deslizamento com folga, senão cada ciclo dá prejuízo."),
    ("Reserva de caixa", "Parte do capital que fica fora da grelha, em USDT."),
    ("Ordem mínima", "O valor mais baixo que a Binance aceita por ordem. Limita quantos degraus cabem no teu capital."),
    ("Deslizamento", "A pequena diferença entre o preço esperado e o preço a que a ordem realmente executa. Aqui é 0,05% contra ti."),
]

_REASONS = [
    ("stop-loss", "O preço ficou abaixo do stop durante vários minutos, por isso o bot fechou tudo para limitar a perda. "
                  "Não retoma sozinho. Vê se o mercado continua a cair antes de criares outro bot."),
    ("queda de", "O preço caiu depressa, mais do que o limite numa hora. O bot parou de comprar para não apanhar a queda a meio; "
                 "as vendas abertas mantêm-se. Retoma quando a queda abrandar."),
    ("limite diário", "O bot perdeu hoje o máximo permitido e parou de comprar. Podes retomá-lo já ou esperar pelo dia seguinte."),
    ("o capital do bot caiu", "O capital do bot caiu o máximo permitido desde o início. Antes de retomar, pensa se esta estratégia "
                              "serve neste mercado."),
    ("PARAR TUDO", "Foi carregado o botão de emergência. O bot fechou a posição (simulada) e cancelou tudo. Não volta a arrancar sozinho."),
    ("pedido do utilizador", "Foste tu que pediste esta ação no painel."),
    ("erro interno", "Houve um erro inesperado e o bot ficou em pausa por segurança. Diz-me para investigar."),
]

BLOCKED = ("Compras suspensas: houve várias compras seguidas sem nenhuma venda, sinal de queda. "
           "Voltam a ser permitidas assim que uma venda executar.")


def reason_help(reason):
    """O que significa o motivo mostrado e o que fazer. None se não houver motivo."""
    for key, text in _REASONS:
        if reason and key in reason:
            return text
    return None
