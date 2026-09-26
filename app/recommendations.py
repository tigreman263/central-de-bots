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
        "summary": "Usa o valor real que pensas investir, para a simulação refletir a realidade.",
        "profiles": [
            ("Conservador", "Igual ao real (~560 €).", None),
            ("Equilibrado", "Igual ao real, e uma segunda corrida de teste com 300 €.", None),
            ("Arrojado", "Mais do que o real. Dá resultados enganadores.", None),
        ],
        "mistake": "Simular com 10 000 € e esperar o mesmo comportamento com 560 €.",
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
