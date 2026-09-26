# Aba Portefólio (desenho, por aprovar)

Só leitura. Os bots continuam só em simulação. Nenhuma função de ordem, venda ou levantamento existe no código. A IA nunca decide: só explica números que o código calculou.

## O que mostra
Só o portefólio real lido da Binance: moeda, quantidade (livre e em ordens), valor em USDT/EUR/USD, peso (% do total) e variação 24 h e 7 dias. No topo: valor total, hora da última leitura, estado da ligação. Poeira (valores minúsculos) escondida por opção. Nada de bots nesta aba.

## Chave de API
- Só leitura, sem trading, sem levantamentos, com IP restrito (a chave é criada por ti na Binance).
- O painel recusa a chave se a Binance indicar permissão de trading ou de levantamento.
- Guardada num ficheiro protegido fora da base de dados, do git e da pasta do projeto. Nunca aparece no ecrã (só os últimos 4 caracteres) nem nos registos.
- Botão "Testar ligação" com erros em português.
- Se falhar ou for revogada: "Sem ligação: dados indisponíveis", com a hora da última leitura boa marcada como antiga. Nunca inventa valores; cria o alerta "Ligação à Binance falhou" e suspende as análises de risco.

## Critérios de "moeda arriscada" (calculados por código; limiares = pontos de partida, ajustáveis na Configuração)
| Critério | Atenção | Alto |
|---|---|---|
| Concentração (só moedas não estáveis; stablecoins contam como reserva) | > 40% do total | > 60% |
| Volatilidade (variação diária média, 30 dias) | > 6% | > 10% |
| Liquidez (volume 24 h) | < 1 M USDT | < 200 k USDT |
| Spread | > 0,3% | > 1% |
| Moeda recente | < 30 dias (info: < 90) | n/a |
| Stablecoin (desvio do valor 1) | > 0,5% | > 2% |
| Monitorização/retirada | só dados automáticos da API (estado de negociação, volume); não lê anúncios na v1 | |

Cada mensagem de recomendação termina com "A decisão é sua".

## Alertas
Aparecem na aba Alertas e como ponto de cor no menu do Portefólio. Cada alerta tem chave única (moeda + critério). Só se repete se a gravidade subir ou passarem 24 h sem ser marcado como visto. Fecha sozinho quando a condição desaparece. Telegram/email depois, com as mesmas regras.

## Capital
O capital e a divisão do saldo passam a usar os valores reais lidos. Se a leitura falhar, usa o último valor bom, marcado como antigo.

## Testes de "funciona" (escritos antes)
- Chave com trading ou levantamento ativo é recusada.
- A chave nunca aparece em base de dados, git, registos ou páginas.
- Com resposta simulada da Binance, os valores e pesos somam 100%.
- Cada critério dispara no nível certo, incluindo o limiar exato.
- Ligação falhada mostra "indisponível" e nunca números novos.
- O mesmo alerta não se repete dentro de 24 h e fecha quando a condição desaparece.
- Não existe nenhuma função de ordem, venda ou levantamento no código.
- PARAR TUDO continua a funcionar.
