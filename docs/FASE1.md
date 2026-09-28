# Fase 1: recuperação e contabilidade (Testnet)

Âmbito: só contabilidade, reconciliação e recuperação dos bots em modo Testnet. **Não** mexia no PARAR TUDO (Fase 2), na
estratégia da grelha, nos parâmetros, no vigilante nem no systemd.

Regra de fundo (igual à v0.3): a **exchange é a fonte da verdade** para ordens, execuções e saldos; a base de dados local
guarda intenção, configuração, estado operacional, histórico, custo e contabilidade. O motor (`engine.py`) só decide;
o executor (`executor.py`) reconcilia e envia; `trader.py` continua a ser o único módulo com POST/DELETE.

## O que mudou

| Peça | Antes | Agora |
|---|---|---|
| Trades | Somados por ordem, só quando a ordem terminava; sem ids | Tabela `bot_trade_registry`: cada trade (id da exchange) contabiliza-se **uma só vez** (`UNIQUE(bot_id, symbol, cid, trade_id)`) |
| Fills parciais | Só contavam no estado terminal | Dinheiro, moeda e comissões contam-se trade a trade; o degrau (posição, ciclo, pó) só muda quando a ordem termina |
| `myTrades` incompleto | Contava-se só o que vinha; nunca se corrigia | `contabilizado < executedQty` numa ordem terminada → estado `settling` (RECONCILIATION_PENDING) + evento + nova tentativa a cada passo. Só ao fim de 6 h há um **ajuste explícito** (linha `adjusted` no registo, sem comissão, alerta alto) |
| Reconcile | Só corria com ordens locais (`executor.py:99`) | Corre sempre (bots a trabalhar), classifica e recupera |
| Reset / ordem desaparecida | `testnet_reset`: pára e limpa; as outras ordens ficavam vivas e órfãs | Estado `RECOVERING`: sem ordens novas, lê a exchange, classifica, contabiliza, reconhece e só depois volta a trabalhar |
| Pó | Custo restante ia para `realized` (perda falsa) | `residual_qty` / `residual_cost` explícitos; nunca é perda realizada; vende-se (com o custo) na liquidação |
| Saldo | Só moeda do par, só "menor"; USDT nunca | Nos dois sentidos, contra o total de **todos** os bots da conta; USDT verificado (só avisa, nunca é gatilho de reset) |
| Restauro de cópia antiga | Ordens com UID antigo nunca reconhecidas; ids repetidos | UID gravado logo na criação; ids `cb…` reconhecidos por bot **e** UID; contador nunca fica atrás dos ids existentes; id repetido com outra ordem recebe id novo |

Migração: `botstore.init()` cria a tabela e as colunas (`booked_qty`, `pending_since`) e, se a base já tinha bots, faz
**antes** uma cópia `app.db.antes-do-registo-AAAAMMDD-HHMMSS.bak`. Bots já existentes recebem `registry_from` (agora):
o que já foi contabilizado antes fica fora da procura de execuções por contabilizar (senão contava-se duas vezes).
Apagar um bot **não** apaga o seu registo de trades.

## Classificação (reconcile)

`KNOWN_MATCH` (igual dos dois lados) · `KNOWN_MISSING` (o bot julga-a aberta, a exchange não a conhece) ·
`UNKNOWN_BOT_ORDER` (aberta, é deste bot mas não há registo local) · `FILLED_NOT_BOOKED` / `CANCELLED_NOT_BOOKED`
(executada, ou cancelada depois de executar, sem contabilizar) · `UNKNOWN_POSITION` (moeda na conta que nenhum bot nem
execução explica: **nunca** é vendida nem adotada).

Uma ordem é **deste bot** só se o id (`cb{bot}{uid}-{degrau}{lado}-{n}`) tiver o número do bot **e** um UID que o bot
conhece. Nunca só pelo prefixo: ordens de outro bot, ou de outra instalação com o mesmo número de bot, ficam intactas.

## RECOVERING

Entra quando há `KNOWN_MISSING`, `UNKNOWN_BOT_ORDER` ou o saldo mudou sem explicação. Durante o estado:

1. Não se criam nem enviam ordens novas (o `flush` só cancela) e as velas não avançam.
2. Ordens locais que a exchange já não conhece saem do registo local (a contabilidade já feita fica).
3. Ordens do bot sem dono local: cancelam-se e conta-se o que já executaram.
4. `allOrders`: ordens do bot já terminadas que nunca foram contabilizadas contam-se agora, pela ordem de criação.
   Se ainda encaixam num degrau da grelha atual (degrau e lado do id, preço igual, degrau no estado certo) fecham esse
   degrau; senão ficam como pó sem degrau.
5. Saldos nos dois sentidos. Se a moeda na Testnet é menos de metade da do bot (e vale mais que a ordem mínima) conclui-se
   que a Testnet foi reposta: o bot pára (`testnet_reset`) **sem** apagar nada financeiro.
6. Só então volta ao estado anterior, reconstrói as ordens e aplica um `pause`/`stop`/PARAR TUDO que tenha chegado entretanto.

Se a rede falha a meio, fica `RECOVERING` (guardado) e continua no passo seguinte. Normalmente começa e acaba no mesmo passo.

## Posição e pó (para a fase 2)

`Engine.position()` devolve `total_qty`, `tradable_qty` (em degraus com venda), `residual_qty` (pó declarado, com
`residual_cost`), `unattributed_qty` e `kind`: `flat` · `closable` · `residual` · `unknown`. Sobrevive a reinício,
reconcile, recentragem e cópia de segurança (vive no estado do bot).

## Invariantes → testes (`tests/test_recovery.py`)

| | Invariante | Teste principal |
|---|---|---|
| I1 | contabilizado ≤ `executedQty` | `check_invariants` em todos os testes, incl. `test_random_partial_and_delayed_fills_keep_every_invariant` |
| I2 | ordem terminada: igual, ou `settling` | `test_delayed_trades_1_then_2_then_3…`, `test_incomplete_my_trades_0_078…`, `…adjusted_explicitly…` |
| I3 | cada `tradeId` uma vez | `test_the_same_trade_is_never_booked_twice` (memória e base de dados) |
| I4 | nenhuma ordem do bot ignorada | `test_zero_local_orders_and_six…`, `test_reset_with_open_orders…` |
| I5 | sem ordens novas em recuperação | `…zero_local…` (cancelamento falha), `test_orders_already_waiting_to_be_sent_are_not_sent_while_recovering` |
| I6 | reset não apaga histórico | `test_the_real_testnet_reset_still_stops_and_keeps_all_history`, `…deleting_a_bot_keeps_its_trade_registry` |
| I7 | pó representado | `test_partial_sale_under_the_minimum…`, `test_residual_survives_a_recenter…` |
| I8 | saldos nos dois sentidos | `test_balance_*` (igual, menor, muito menor, maior, USDT, dois bots no mesmo par) |
| I9 | local = exchange no fim | `test_random_…` (identidade base local = exchange − por contabilizar), `test_restoring_an_old_backup…` |

Os testes foram validados por **mutação**: reintroduzir cada bug antigo (trade sem deduplicar, parciais só no fim,
reconcile sem ordens locais, trades em falta disfarçados, pó como perda, envio em RECOVERING, saldo maior ignorado,
apagar registo ao apagar o bot) faz falhar pelo menos um teste.

## Limites conhecidos (a documentar, não a esconder)

- **Restauro:** um UID só se reconhece se estiver na cópia restaurada. O UID grava-se na criação do bot, por isso qualquer
  cópia feita depois de o bot existir o conhece. Uma cópia **anterior** à criação do bot, com o mesmo número de bot
  reutilizado, deixa ordens com um UID desconhecido: ficam intactas (podem ser de outra instalação) até ligares o UID:
  `botstore.link_prefix(conn, bot_id, "abc123")`. Ordens de compra inicial/liquidação (`-1i`, `-1l`) recuperadas
  ficam como pó sem degrau.
- **Trades depois de um reset da Testnet:** os ids de trade recomeçam. A chave inclui o id da ordem (`cid`, único por
  bot), por isso não colidem com os antigos.
- **Restauro de uma base anterior à Fase 1:** o que a exchange fez entre a cópia e o restauro, em ordens que a cópia não
  conhecia, só é contabilizado se estiver depois de `registry_from` (o arranque da migração).
- **Bots parados sem trabalho pendente** não são vigiados (nem por reconcile nem por saldo).
- **PARAR TUDO durante RECOVERING:** na Fase 1 ficava guardado até a recuperação acabar. Desde a Fase 2 entra logo em
  STOPPING, que já inclui a reconciliação completa (ver `docs/FASE2.md`).
- **Ordens terminadas em `settling` sem degrau próprio** (só aparecem em ordens cancelar-e-recuperar) tratam-se como pó.
- **Contas partilhadas:** dois bots no mesmo par (ex.: XRPUSDT) funcionam e o saldo é comparado com o total de todos os
  bots, mas partilham a mesma carteira. Decisão em aberto (fase 3): um só bot ativo por par, ou reserva por bot.
- Comissões na Testnet vêm a 0; o ajuste explícito também assume 0 (na Binance real seria pedir o valor à exchange).
- Confirmar contra a Testnet real (marcado "a confirmar"): peso do `allOrders` (20), `myTrades` por `orderId` sem
  `fromId` (o desenho só usa `orderId`) e devolução de `tradeId` em `fills`.
