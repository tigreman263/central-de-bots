"""Recomendações por campo da Configuração (redigidas por agente especialista).

Todos os números são pontos de partida a validar em simulação. Não são garantia.
Cada perfil traz `apply`: valores que o botão "Aplicar" preenche no formulário
(nada é guardado sem passar pelo resumo "antes → depois").
"""

GENERAL = [
    "Muda um parâmetro de cada vez e anota o motivo e o resultado.",
    "Não aumentes o risco depois de ganhos, nem depois de perdas.",
    "Revê semanalmente ou mensalmente, não a cada hora.",
    "Nas chaves da Binance, nunca atives levantamentos. Só leitura e trading, com lista de IPs permitidos.",
    "Testa bastante em simulação, incluindo períodos maus, antes de qualquer dinheiro real. "
    "Resultados passados não garantem resultados futuros e o capital pode perder-se.",
]

DISCLAIMER = "Todos os números são pontos de partida a validar em simulação, não garantias nem promessa de lucro."

FIELDS = {
    "capital": {
        "summary": "O valor deve ser o mais parecido possível com o que tens mesmo na carteira, para a simulação "
                    "refletir a realidade — não é um número a copiar de um exemplo.",
        "profiles": [],
        "mistake": "Simular com um valor muito diferente do real (ex.: 10 000 € quando tens 560 €) dá resultados "
                   "enganadores: as ordens mínimas e as comissões pesam de forma muito diferente consoante o capital.",
        "small": "Com pouco capital, as ordens mínimas (~5-10 USDT) e as comissões (~0,1% por operação) pesam muito mais. "
                 "Poucas ordens maiores compensam mais do que muitas pequenas.",
    },
    "split": {
        "summary": "Só o trading corre risco. O resto protege-o.",
        "profiles": [
            ("Conservador", "70/20/10: só ~112 € em risco.",
             {"split_reserve": 70, "split_trading": 20, "split_cash": 10}),
            ("Equilibrado", "60/30/10: ~168 € em risco.",
             {"split_reserve": 60, "split_trading": 30, "split_cash": 10}),
            ("Arrojado", "40/50/10: ~280 € em risco, perdas maiores e mais ansiedade.",
             {"split_reserve": 40, "split_trading": 50, "split_cash": 10}),
        ],
        "mistake": "Pôr quase tudo em trading porque a simulação correu bem.",
        "small": "Com 20% de trading são ~112 €. Dividido por vários bots, cada um fica abaixo das ordens mínimas. "
                 "Começa com um só bot.",
    },
    "max_loss_trade": {
        "summary": "Começa pequeno. Uma sequência de perdas não deve estragar a conta.",
        "profiles": [
            ("Conservador", "0,5-1%.", {"max_loss_trade": 1}),
            ("Equilibrado", "1-2%.", {"max_loss_trade": 2}),
            ("Arrojado", "3-5%: poucas perdas seguidas já doem.", {"max_loss_trade": 3}),
        ],
        "mistake": "Subir o limite depois de uma perda, para \"recuperar\".",
        "small": "1% de 168 € são cerca de 1,70 €. Com comissões e deslizamento (diferença de preço na execução), "
                 "um limite tão apertado pode ser ativado por ruído normal. Testa antes de o apertares.",
    },
    "profit_to_reserve": {
        "summary": "Guardar parte dos ganhos protege o que já ganhaste.",
        "profiles": [
            ("Conservador", "70%.", {"profit_to_reserve": 70}),
            ("Equilibrado", "50%.", {"profit_to_reserve": 50}),
            ("Arrojado", "20-30%: cresce mais depressa, mas arrisca mais.", {"profit_to_reserve": 25}),
        ],
        "mistake": "Reinvestir tudo e voltar a expor os lucros.",
        "small": "Com ganhos pequenos (cêntimos ou poucos euros), as transferências podem não compensar por causa das "
                 "comissões. Considera acumular e transferir por períodos.",
    },
    "pause_drawdown": {
        "summary": "É o travão de emergência. Define-o antes de começar, não durante uma queda.",
        "profiles": [
            ("Conservador", "5-10%.", {"pause_drawdown": 10}),
            ("Equilibrado", "15-20%.", {"pause_drawdown": 20}),
            ("Arrojado", "30-50%: perde-se muito antes de parar.", {"pause_drawdown": 35}),
        ],
        "mistake": "Desativar a pausa, ou ignorá-la e retomar de imediato.",
        "small": "Com ~168 € de trading, 10% são ~17 €. Uma variação normal pode parar o bot cedo. "
                 "Ajusta depois de veres quantas paragens falsas houve em simulação.",
    },
    "pair_min_volume": {
        "summary": "Volume negociado nas últimas 24 h, em milhões de USDT. Mais volume = ordens executadas sem mexer no preço.",
        "profiles": [
            ("Conservador", "50 M ou mais: só pares muito líquidos (BTC, ETH e pouco mais).", {"pair_min_volume": 50}),
            ("Equilibrado", "20 M (ponto de partida).", {"pair_min_volume": 20}),
            ("Arrojado", "5 M: aparecem mais pares, com ordens menos fáceis de executar.", {"pair_min_volume": 5}),
        ],
        "mistake": "Baixar muito o volume só para ter mais pares: com pouca liquidez o preço salta e a grelha executa mal.",
        "small": "Com capital pequeno as tuas ordens são minúsculas: 5 M já chega para não mexeres no preço. Mas se o mercado "
                 "ficar calmo, o volume total baixa para todos; se nada aparecer, baixa este valor em vez de forçar um par mau.",
    },
    "pair_max_spread": {
        "summary": "Diferença entre o preço de compra e o de venda, em %. Cada ciclo paga-a; um spread grande come o ganho.",
        "profiles": [
            ("Conservador", "0,02%.", {"pair_max_spread": 0.02}),
            ("Equilibrado", "0,05% (ponto de partida).", {"pair_max_spread": 0.05}),
            ("Arrojado", "0,1%: mais pares, mas cada ciclo custa mais.", {"pair_max_spread": 0.1}),
        ],
        "mistake": "Ignorar o spread: um degrau de 1,5% com spread de 0,3% perde uma quinta parte do ganho.",
        "small": "Com ganhos por ciclo de cêntimos, o spread decide se compensa. Prefere pares com spread perto de 0,01%.",
    },
    "pair_range_min": {
        "summary": "Movimento mínimo nas últimas 24 h (máximo menos mínimo, em % do preço). Abaixo disto o preço está parado e a "
                   "grelha quase não faz ciclos.",
        "profiles": [
            ("Conservador", "2% (ponto de partida).", {"pair_range_min": 2}),
            ("Equilibrado", "1,5%.", {"pair_range_min": 1.5}),
            ("Arrojado", "1%: aceita mercados mais parados.", {"pair_range_min": 1}),
        ],
        "mistake": "Subir demasiado o mínimo em mercados calmos: deixa de haver pares e acabas a forçar critérios.",
        "small": "Uma grelha ganha quando o preço anda de um lado para o outro; com pouco movimento ganha pouco, mas também "
                 "arrisca pouco.",
    },
    "pair_range_max": {
        "summary": "Movimento máximo nas últimas 24 h. Acima disto o preço anda demasiado depressa e a grelha fica presa numa "
                   "queda ou deixa de ganhar numa subida.",
        "profiles": [
            ("Conservador", "6%.", {"pair_range_max": 6}),
            ("Equilibrado", "8% (ponto de partida).", {"pair_range_max": 8}),
            ("Arrojado", "12%: aceita pares mais voláteis, com mais risco.", {"pair_range_max": 12}),
        ],
        "mistake": "Aceitar \"a moeda que mais mexe\": é a que mais depressa sai da zona da grelha.",
        "small": "Quando o mercado inteiro está agitado, quase todos os pares passam o máximo. Não subas o limite por causa de um "
                 "dia: espera, ou deixa a opção de alargar automaticamente fazer isso de forma visível.",
    },
    "pair_range_target": {
        "summary": "O movimento que consideras ideal: quanto mais perto deste valor, mais pontos o par recebe (40% da nota).",
        "profiles": [
            ("Conservador", "3%.", {"pair_range_target": 3}),
            ("Equilibrado", "4% (ponto de partida).", {"pair_range_target": 4}),
            ("Arrojado", "6%.", {"pair_range_target": 6}),
        ],
        "mistake": "Pôr o ideal fora do intervalo mínimo-máximo (o painel recusa).",
        "small": "É só uma preferência de ordenação: não exclui nenhum par, escolhe qual aparece primeiro.",
    },
    "pair_adaptive": {
        "summary": "Se nenhum par cumprir os critérios, o painel alarga-os por degraus (e diz que o fez). Ligada, nunca ficas "
                   "sem opções; desligada, ficas sem sugestões e o painel diz porquê.",
        "profiles": [
            ("Conservador", "Desligada: só pares que cumprem exatamente o que definiste.", None),
            ("Equilibrado", "Ligada (ponto de partida): a lista nunca fica vazia, e os pares alargados vêm marcados.", None),
        ],
        "mistake": "Escolher um par marcado \"fora dos critérios\" sem ler porquê.",
        "small": "Serve para os dias em que o mercado muda muito e os teus números deixam de existir. Os degraus são: volume "
                 "a 50%, depois 20%; spread ×1,5, depois ×3; e movimento alargado nos dois lados.",
    },
    "exclusions": {
        "summary": "Mantém memecoins e moedas alavancadas excluídas. Stablecoins só na caixa.",
        "profiles": [
            ("Conservador", "Evitar tudo.", {"exclusions": ["memecoins", "leveraged", "stables"]}),
            ("Equilibrado", "Evitar tudo.", {"exclusions": ["memecoins", "leveraged", "stables"]}),
            ("Arrojado", "Permitir uma memecoin com limite pequeno, só depois de bons resultados em simulação.",
             {"exclusions": ["leveraged", "stables"]}),
        ],
        "mistake": "Excluir por moda ou permitir por \"esta vai subir\". Em grelha, moedas muito voláteis saem da zona "
                   "e ficam presas.",
        "small": "Com 560 €, prefere pares grandes e líquidos (BTC, ETH). O spread (diferença entre compra e venda) "
                 "em moedas pequenas come o ganho.",
    },
}


# ---------- limiares de risco do portefólio ----------
# Cada item: o que é, unidade, e 3 perfis (valores de atenção e alto). Pontos de partida a validar com alertas reais.
RISK_ITEMS = [
    {"key": "risk_conc", "title": "Concentração de uma moeda", "unit": "% do portefólio",
     "attn_key": "risk_conc_attn", "high_key": "risk_conc_high",
     "attn_label": "Avisar (atenção) a partir de", "high_label": "Avisar (alto) a partir de",
     "what": "Quanto do valor total pode estar numa só moeda antes de o painel avisar. Stablecoins não contam.",
     "profiles": [("Conservador", "25% e 40%", 25, 40), ("Equilibrado", "40% e 60%", 40, 60),
                  ("Arrojado", "55% e 75%", 55, 75)],
     "mistake": "Ignorar o aviso porque a moeda \"tem subido\". É quando pesa mais que uma queda dói mais.",
     "small": "Com poucas moedas relevantes é normal ter uma com muito peso. O aviso serve para o decidires de propósito, não para vender."},
    {"key": "risk_vol", "title": "Volatilidade diária", "unit": "% de variação média por dia (30 dias)",
     "attn_key": "risk_vol_attn", "high_key": "risk_vol_high",
     "attn_label": "Avisar (atenção) acima de", "high_label": "Avisar (alto) acima de",
     "what": "Quanto o preço da moeda costuma variar num dia. Quanto maior, mais depressa se ganha e mais depressa se perde.",
     "profiles": [("Conservador", "4% e 7%", 4, 7), ("Equilibrado", "6% e 10%", 6, 10), ("Arrojado", "9% e 15%", 9, 15)],
     "mistake": "Baixar o limiar até quase tudo avisar. Alertas a mais fazem-te ignorar os que importam.",
     "small": "Moedas pequenas oscilam muito mais que BTC ou ETH. Confirma se o peso delas na carteira é o que queres."},
    {"key": "risk_liq", "title": "Liquidez (volume em 24 horas)", "unit": "USDT negociados por dia",
     "attn_key": "risk_liq_attn", "high_key": "risk_liq_high",
     "attn_label": "Avisar (atenção) abaixo de", "high_label": "Avisar (alto) abaixo de",
     "what": "Quanto dinheiro se negoceia na moeda num dia. Com pouco volume pode ser difícil vender ao preço esperado. Aqui \"alto\" é um valor mais baixo que \"atenção\".",
     "profiles": [("Conservador", "5 000 000 e 1 000 000", 5000000, 1000000),
                  ("Equilibrado", "1 000 000 e 200 000", 1000000, 200000),
                  ("Arrojado", "300 000 e 50 000", 300000, 50000)],
     "mistake": "Pôr o limiar muito baixo e deixar de ver moedas que ninguém compra.",
     "small": "As tuas ordens são pequenas, por isso o volume conta menos, mas uma moeda sem compradores pode ficar presa."},
    {"key": "risk_spread", "title": "Spread (diferença entre compra e venda)", "unit": "% do preço",
     "attn_key": "risk_spread_attn", "high_key": "risk_spread_high",
     "attn_label": "Avisar (atenção) acima de", "high_label": "Avisar (alto) acima de",
     "what": "A diferença entre o preço a que se compra e o preço a que se vende. Vender custa logo essa diferença.",
     "profiles": [("Conservador", "0,1% e 0,5%", 0.1, 0.5), ("Equilibrado", "0,3% e 1%", 0.3, 1),
                  ("Arrojado", "0,5% e 2%", 0.5, 2)],
     "mistake": "Achar que só as comissões contam. O spread também é custo, sobretudo em moedas pequenas.",
     "small": "Com montantes pequenos, um spread de 1% come mais que a comissão de 0,1%."},
    {"key": "risk_stable", "title": "Desvio da stablecoin", "unit": "% afastado de 1 USDT",
     "attn_key": "risk_stable_attn", "high_key": "risk_stable_high",
     "attn_label": "Avisar (atenção) acima de", "high_label": "Avisar (alto) acima de",
     "what": "Uma stablecoin devia valer sempre cerca de 1 USDT. Se se afastar, pode ter perdido a paridade e talvez não recupere.",
     "profiles": [("Conservador", "0,3% e 1%", 0.3, 1), ("Equilibrado", "0,5% e 2%", 0.5, 2), ("Arrojado", "1% e 4%", 1, 4)],
     "mistake": "Assumir que uma stablecoin nunca desvia. Já houve casos em que perderam a paridade.",
     "small": "O teu USDC e a tua caixa em stablecoins valem pouco, mas é a parte que devia ser mais segura."},
    {"key": "risk_young", "title": "Moeda recente", "unit": "dias de histórico",
     "attn_key": "risk_young_attn", "high_key": "risk_young_info",
     "attn_label": "Atenção se tiver menos de", "high_label": "Info se tiver menos de",
     "what": "Há quantos dias a moeda existe na Binance. Moedas novas têm comportamento pouco conhecido. Aqui o primeiro valor é o mais curto.",
     "profiles": [("Conservador", "60 e 180 dias", 60, 180), ("Equilibrado", "30 e 90 dias", 30, 90),
                  ("Arrojado", "14 e 45 dias", 14, 45)],
     "mistake": "Confundir moeda nova com moeda com potencial. Pouca história também é pouca informação.",
     "small": "Se tens moedas pequenas por curiosidade, este aviso ajuda a saber quais são as mais recentes."},
]
