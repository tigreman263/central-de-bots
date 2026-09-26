# Proposta gráfica do painel (aprovada e implementada)

Estado (2026-09-26): estilo A aprovado e implementado. Acrescentado depois: página Bots > Ligações, gráfico de preço por bot (compras ▲, vendas ▼, stop, eventos), selo TESTNET/SIMULAÇÃO, confirmação ao parar um bot, estados com símbolo (o "parado" já não é vermelho).

## Navegação
- 6 secções fixas: Início, Bots, Reserva, Estatísticas, Alertas, Configuração.
- PC: barra lateral. Telemóvel: barra inferior com 5 separadores (Alertas e Configuração em "Mais"). Ponto de cor indica alertas ou propostas por aprovar.
- Barra de topo fixa: selo de modo, botão PARAR TUDO, seletor USDT/EUR/USD.

## Ecrãs, por ordem de importância
- **Início:** estado geral numa frase; valor total e lucro líquido de hoje; barra reserva/trading/caixa; bots com estado e lucro; pendentes (propostas, alertas); PARAR TUDO visível.
- **Bots:** cartões com nome, moeda, estado, lucro líquido, Pausar/Parar. "Criar bot" em 3 passos: estratégia (v1: Grelha), moeda e montante, risco com resumo final.
- **Reserva:** proposta em cartão (par, justificação curta, risco, montante); Aprovar/Rejeitar; histórico por baixo.
- **Estatísticas:** evolução do valor (1D/1S/1M/Tudo), reserva acumulada, desempenho por bot; tudo líquido de comissões (bruto/comissões/líquido na dica).
- **Alertas:** lista cronológica com gravidade, filtro, "marcar como lido"; registos técnicos em "Detalhes".
- **Configuração:** secções recolhíveis com valores por defeito e explicação de uma linha; sliders com a soma de 100% em tempo real; exclusões de moedas por categoria com interruptores.

## Estilo visual
Escolhido: **A, "calmo e limpo"** — espaço branco, cartões grandes, uma cor de destaque azul, gráficos simples; tema claro, escuro ou automático.
Cores: lucro verde-azulado, perda vermelho-coral, alerta âmbar, informação azul, neutro cinzento. Lucro/perda nunca só por cor (sinal +/− e seta). Contraste mínimo 4.5:1, texto mínimo 16 px, alvos táteis mínimo 44 px. Vermelho forte só para PARAR e ações destrutivas.

## Modo simulação
Faixa fixa no topo, âmbar/roxo, "MODO SIMULAÇÃO – Testnet – dinheiro fictício"; nunca escondível; valores com etiqueta "simulado"; marca de água nos gráficos.

## Telemóvel
Uma coluna, cartões empilhados, barra inferior, PARAR TUDO acessível no topo mas afastado de botões comuns; formulários por passos.

## Evitar erros em ações de risco
- Pausar: 1 toque, "Desfazer" durante 5 s.
- Parar bot: confirmação com consequências ("Vai cancelar 4 ordens abertas").
- PARAR TUDO: janela clara, um único confirmar grande, sem escrever texto.
- Apagar bot ou alterar risco/divisão do saldo: resumo "antes → depois" e botão com o verbo específico.
- Botão perigoso separado do seguro; "Cancelar" realçado por defeito.
- Alterações só se aplicam ao Guardar, com pré-visualização do impacto.
- Ações registadas nos alertas/registos; erros em português simples.
