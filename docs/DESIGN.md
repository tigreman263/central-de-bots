# Design v1 — Plataforma de bots Binance (aprovado; implementado até à v0.3)

Estado (2026-09-26): aprovado pelo utilizador e implementado até à v0.3 (painel, portefólio, grelha em simulação e na Testnet). Ainda por fazer deste desenho: 50% do lucro para a reserva, alertas por email, "desfazer" ao pausar, gestor de saldo que aplica a divisão reserva/trading/caixa, agente de moedas (v0.4). Só Testnet e paper trading. A IA nunca envia ordens.

## 1. O que é
Programa em Python que corre 24/7 num Raspberry Pi, com painel web. Dinheiro simulado. A IA só ordena e justifica sugestões.

## 2. Decisões fechadas com o utilizador
- Usa o saldo que já tem na Binance, sem depósitos novos.
- Divisão reserva/trading/caixa decidida por ele depois (sugestão inicial 60/30/10), configurável no painel.
- ~50% do lucro realizado vai para a reserva; pausa se o trading cair ~20% (pontos de partida a validar).
- v1: um bot de grelha, 1 par spot, sem alavancagem. Seguir tendência vem depois, como bot separado; a estratégia escolhe-se por bot na configuração.
- Perda máxima ~2% por operação, com limite diário e paragem automática.
- Reserva: um algoritmo (código com dados da Binance) propõe pares; a IA só ordena e justifica; o utilizador aprova cada alteração. Exclusões por categoria.
- Valores em USDT, com EUR e USD no painel. Lucro sempre líquido de comissões.
- Acesso só em casa com palavra-passe; arquitetura pronta para acesso externo depois (VPN).
- Alertas no painel + Telegram/email.
- Validação: testes com histórico + 4 semanas em paper, critérios escritos antes. Dinheiro real fora da v1.

## 3. Componentes
- **Motor de bots:** corre cada bot.
- **Guarda de risco:** único caminho para enviar ordens; valida 2% por operação, limite diário e paragem automática.
- **Ligação à Binance:** lê preços/saldo e envia ordens (Testnet ou simulador paper).
- **Gestor de saldo:** divide reserva/trading/caixa e aplica a regra do lucro.
- **Agente da reserva:** propõe pares; o utilizador aprova.
- **Painel web:** fala com o motor por base de dados e comandos (ligar/pausar/parar, paragem geral).
- **Alertas:** painel + Telegram/email.

Fluxo: painel grava configuração → motor lê → guarda de risco valida → ordem sai → resultado na base de dados → painel mostra.

## 4. Dados guardados (base de dados local)
Configuração; ordens e execuções (com comissões); posições e estado de cada bot; evolução do valor e reserva acumulada; propostas do agente e decisão; alertas; registo de ações. Chaves da Binance fora da base de dados, em ficheiro protegido (permissão só de negociação, nunca levantamentos).

## 5. Versões e critério de "funciona"
- **v0.1 Painel:** sem palavra-passe não se vê nada; a configuração sobrevive a reiniciar o Pi; mostra saldo e preços em USDT/EUR/USD; botão de paragem geral visível.
- **v0.2 Bot em simulação:** corre 7 dias sem intervenção; nenhuma operação excede ~2% de perda; o limite diário pára o bot; lucro já líquido de comissões.
- **v0.3 Ordem na Testnet:** retoma sem duplicar ordens após desligar o Pi (5 testes); alertas chegam; paragem geral cancela tudo em segundos.
- **v0.4 Agente sugere moedas:** nada muda sem aprovação; exclusões respeitadas; cada proposta mostra os dados usados.
- **Validação final:** histórico + 4 semanas em paper com critérios escritos antes (lucro líquido positivo, perda máxima diária nunca ultrapassada, zero ordens duplicadas).

## 6. Riscos
- Paper/Testnet parece melhor que a realidade: simular comissões e deslizamento.
- Falha do Pi/internet: pára posições novas, guarda estado, retoma sem duplicar.
- Grelha perde em tendência forte: limites de perda e paragem automática.
- Chaves só com permissão de negociação; painel só na rede local.
- Sugestões da IA enganadoras: o código decide candidatos, o utilizador aprova.
- Alertas a falhar em silêncio: verificar periodicamente.

## 7. Decisões fechadas (2026-09-25)
1. **Primeiro bot:** o par é sugerido pelo agente por atividade. O código filtra liquidez alta, spread apertado e volatilidade moderada; o agente ordena e justifica; o utilizador aprova. O bot usa metade do capital de trading.
2. **Capital:** carteira de ~560 EUR. A divisão reserva/trading/caixa não fica fixa: o utilizador define-a no painel (a simulação parte de 60/30/10, editável). Limites de ~20% (pausa) e ~2% (por operação) ficam como pontos de partida configuráveis.
3. **Critério das 4 semanas em paper:** lucro líquido de comissões acima de zero, perda diária nunca acima do limite e zero ordens duplicadas. A passagem à fase seguinte é decidida pelo utilizador.

Nota: com este capital, a ordem mínima da Binance (~5-10 USDT, a confirmar) e as comissões limitam o número de degraus da grelha; a v1 serve sobretudo para aprender e validar.
