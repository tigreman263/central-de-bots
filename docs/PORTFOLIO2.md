# Portefólio reorganizado, Staking, Ai e custo médio (desenho, por aprovar)

O código calcula tudo. A IA, quando existir, só explica. Por agora a chave é só de leitura e o painel nunca envia ordens.

## Decisões do utilizador (2026-09-25)
- **Staking:** por agora só ver o que tem e receber sugestões; ele subscreve na Binance.
- **Futuro:** mais tarde os bots poderão fazer tudo menos levantamentos (incluindo subscrever staking e negociar). Será uma fase própria, com aprovação explícita nessa altura, chave com permissões de escrita mas nunca de levantamentos (ideal com IP restrito) e a guarda de risco a validar tudo.
- **Custo médio:** manual por moeda primeiro (ex.: ADA 0,75 USDT); importação de CSV do histórico da Binance mais tarde, para confirmar.
- **Regra "nunca vender abaixo do custo médio":** opcional por bot, desligada por defeito, com aviso do risco e alerta após limite de tempo ou de perda; nunca vende sozinha por isso.
- **Detalhe para a futura IA:** valores completos da carteira, sem chaves nem IDs de conta.

## 1. Portefólio reorganizado
- **Topo:** cartões de resumo: valor total, variação 24 h, lucro/prejuízo total (com custo médio), % em stablecoins, % em Earn, nº de alertas.
- **Corpo em secções recolhíveis:** Moedas principais, Stablecoins, Earn (uma linha por moeda: livre + em Earn), Poeira (escondida por defeito mas contada no total).
- **Ordenar** por valor, variação, lucro/prejuízo ou nome. **Filtros:** só com alertas, só com Earn, esconder poeira.
- **Riscos ao lado da moeda:** selo na linha (ex.: "concentração alta"); a caixa de riscos passa a ser só um resumo com ligações.
- **Telemóvel:** tabela vira lista de cartões; resumo em 2x2; secções recolhidas por defeito.

## 2. Aba Staking
- **O que tenho:** posições Simple Earn flexível e bloqueado, com montante, APR, prazo, data de fim e rendimento anual estimado (leitura).
- **Sugestões (calculadas por código):** APR, prazo/bloqueio, liquidez, risco da moeda, % do saldo que ficaria preso; cada sugestão com pontuação e razão em frase simples.
- **Regras de segurança:** nunca sugerir prender mais de X% da moeda; nunca sugerir bloqueado em moeda com alerta de risco; preferir flexível se houver bots a usar a moeda.
- **Avisos honestos:** APR promocional acaba, APR não é garantido, o preço da moeda pode cair mais do que o rendimento.
- **Limite:** o painel só sugere e explica o que clicar; subscrever é escrita e fica fora por agora. A cobertura depende do que a API lista.

## 3. Aba "Ai"
- **Análise por código, em blocos:** desempenho (7/30 dias), concentração (top 3), risco (volatilidade, stablecoins vs. resto), diversificação, staking (parado vs. a render), lucro/prejuízo por moeda, e lista de sugestões de melhoria ordenadas por importância, cada uma com o número que a justifica.
- **Chat com IA:** espaço reservado, desativado ("ainda não ligado").
- **Preparação sem instalar IA:** um resumo estruturado e versionado do portefólio (valores completos, sem chaves, endereços nem IDs de conta). Um verificador confirma que não contém segredos antes de qualquer envio. O envio será sempre manual e visível (botão "perguntar"). A IA futura só recebe o resumo, não tem ferramentas nem acesso à chave, e a resposta nunca vira ordem.

## 4. Custo médio
- Por moeda, escrito à mão, com data da última alteração; guardado localmente. Moedas sem custo mostram "sem custo".
- Lucro/prejuízo não realizado = (preço atual − custo médio) × quantidade, em valor e %. Verde/vermelho com sinal e seta.
- Aviso: "vender agora fixa prejuízo de X" (estimativa; comissões incluídas como estimativa).
- Exemplo real: ADA com custo 0,75 USDT.
- Regra opcional por bot descrita acima; indicador "bloqueado pela regra há N dias".

## Testes de "funciona" (escritos antes)
- Custo médio guardado e editável; sem custo nunca mostra valor inventado.
- ADA a 0,75 com preço abaixo mostra prejuízo negativo e o aviso; com preço acima mostra lucro.
- As secções somam o mesmo total do resumo; a poeira conta no total.
- Os selos de risco aparecem na moeda certa.
- Staking: posições lidas com resposta simulada; sugestões nunca propõem bloqueado em moeda com alerta; nenhuma função de subscrição existe.
- O resumo para a IA não contém chave, segredo nem IDs (teste de verificação), e não sai sem o botão.
- Nenhuma função de ordem, venda, subscrição ou levantamento no código.
