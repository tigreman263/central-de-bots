# Fase 2: PARAR TUDO seguro (Testnet)

Âmbito: só a paragem de emergência (e o pedido de paragem de um bot). Não mexe na estratégia da grelha, nos parâmetros,
no systemd nem no vigilante além do que a paragem precisa.

Princípio: depois de o sistema tomar conhecimento do pedido de paragem **não nasce nenhuma ordem de estratégia**, e um bot
só é dado como parado quando a exchange o confirma. Ligar a flag já não é "parado": é o início de um processo.

## Estados

```text
RUNNING / PAUSED / RECOVERING
        │  PARAR TUDO (flag) ou comando "stop" ou stop-loss
        ▼
    STOPPING ──────────────────────────────────────────────┐
        │ cancelar ordens abertas (fila persistente)        │ sem rede / cancel falha / venda falha:
        │ reconciliar (fills, parciais, ordens sem dono)    │ continua STOPPING, estado guardado,
        │ fechar a posição do bot (MARKET SELL de emergência)│ tenta outra vez; prazo → alerta CRÍTICO
        │ reconciliar outra vez e CONFIRMAR                 │
        ▼                                                   │
    STOPPED  (stop_outcome: closed | residual | position_kept)
```

- `STOPPING` ("A parar"): estado novo, guardado na base de dados. Sobrevive a reinício e nunca volta a `RUNNING`, nem
  que a flag seja desligada (a paragem já está em curso).
- `STOPPED` + `stop_outcome`:
  - `closed`: sem ordens, posição fechada.
  - `residual` (o "STOPPED_WITH_RESIDUAL"): sobrou moeda abaixo do mínimo negociável. Fica no bot como `residual_qty`
    com `residual_cost`, sem contar como perda (Fase 1). Não se tenta vender para sempre.
  - `position_kept`: a regra "nunca vender abaixo do custo" impediu a venda; só se cancelaram as ordens.
- Bots em simulação não têm exchange para confirmar: continuam a passar a `STOPPED` de imediato.

## Fluxo real

1. **Fase 1 (só base de dados, sem rede):** `emergency_sweep` põe todos os bots ativos em `STOPPING` (ou `STOPPED` se forem
   simulados). Um bot com erro ou lento não impede os outros de entrarem em STOPPING.
2. **Fase 2 (por bot, com um fecho por bot):** `_stop_rounds` repete até 6 voltas de:
   reconcile → gravar → cancelar/enviar → repetir. O reconcile da paragem é o da Fase 1 (`_recover`): contabiliza fills
   (também os de cancelamentos que responderam `UNKNOWN_ORDER`), cancela ordens do bot sem dono local, conta o que
   foi executado sem registo, mede saldos.
3. `_stop_step` (executor) decide: enquanto houver fila de cancelamentos, ordens `sending`/abertas/parciais/`settling` ou
   ordens do bot na exchange, **espera**. Sem nada disso: se há posição vendável cria a ordem de emergência
   (`-1l`, MARKET SELL, id único gravado ANTES de enviar, trades pelo registo normal); se não há, `finish_stop` confirma.
4. Sem ligação ou com erro: o bot fica `STOPPING` com o aviso, e o vigilante (~5 s) ou o passo seguinte continuam.

Quantidade vendida = `min(moeda local do bot, moeda da conta − o que os outros bots dizem ter)`, arredondada ao passo.
Nunca mais do que o bot tem, nem do que existe. Moeda na conta que ninguém explica (`UNKNOWN_POSITION`) **não se vende**:
gera alerta (`guard`) e o bot termina só com o que é dele.

## Bloqueio de novas ordens e a corrida stop/POST

Antes de cada POST de uma ordem de estratégia o executor lê o pedido de paragem **na base de dados** (flag PARAR TUDO ou
comando `stop` do bot), não o valor lido no início do passo:

```text
1.ª leitura (antes de preparar) → 2.ª leitura (imediatamente antes do POST) → POST → 3.ª leitura
```

- 1.ª ou 2.ª leitura positivas: a ordem não sai; o bot entra em STOPPING e a ordem vai para a fila de cancelamentos
  (pode ter chegado antes: o cancelamento consulta-a).
- 3.ª leitura positiva (a paragem chegou **durante** o POST): evento `ORDER_IN_FLIGHT_DURING_STOP` (alerta alto), a ordem já
  enviada entra no cancelamento e na reconciliação.
- A janela entre a última leitura e o envio HTTP não se elimina; o que a fecha é a 3.ª leitura + o cancelamento e a
  reconciliação seguintes. Em STOPPING `_sync_orders` não corre, o motor não processa velas e o `flush` só envia a ordem de
  emergência.

## Prazo (deadline)

`emergency_deadline_s` (definições; por defeito **120 s**; 0 desliga). Passado o prazo desde o pedido, e depois de cada prazo
seguinte, sai um evento `guard` "CRÍTICO: … NÃO está parado" (alerta alto). **Não** declara STOPPED: o bot continua STOPPING
e a tentar. Para mudar: `db.set_many(conn, {"emergency_deadline_s": "60"})`.

## Alertas fora do caminho crítico

`notify.start_worker()` (ligado em `run_bots.py`) faz `broadcast` só pôr numa fila (200) e um fio de segundo plano envia.
Um Telegram lento já não atrasa a paragem dos bots seguintes. Sem o fio (testes) o envio continua síncrono.
Os eventos `recon` (uma linha por ordem) não geram alerta; `recovery` e `stop` sim.

## Concorrência

Um `bot_lock(bot_id)` por bot (em vez do fecho global): `tick_bot`, `emergency_sweep` e a recuperação nunca mexem ao mesmo
tempo no mesmo bot, mas um bot lento a falar com a exchange não trava os outros. O `runner.lock` (um só corredor) mantém-se.

## Painel

- Depois de PARAR TUDO: "Paragem pedida. Os bots estão a parar…" (já não "Tudo parado").
- Barra de topo: `PARAR TUDO ATIVO · A PARAR n bots · Reativar` enquanto houver bots por confirmar; `SISTEMA PARADO` só
  quando todos estão parados e sem trabalho pendente.
- Cada bot mostra "⏳ A parar", quantas ordens faltam cancelar e a posição; parado com pó mostra a nota do resíduo.

## Invariantes → testes (`tests/test_emergency.py`)

| | Invariante | Testes |
|---|---|---|
| STOP-1 | depois de observado o stop, nenhuma ordem de estratégia nova | E1, E2, E3, E4, E18, E15 |
| STOP-2 | sem STOPPED com ordens abertas do bot | E8, E9 |
| STOP-3 | sem STOPPED com posição fechável | `test_stop3_…` |
| STOP-4 | nunca vender UNKNOWN_POSITION | E14, E14b |
| STOP-5 | parciais e fills durante o cancelamento contam uma vez | E6, E6b, E7 |
| STOP-6 | falha de cancelamento mantém STOPPING | E8 |
| STOP-7 | Binance offline mantém STOPPING | E9, E17 |
| STOP-8 | reinício em STOPPING não volta a RUNNING | E12 |
| STOP-9 | alertas não bloqueiam | E10, E11b |
| STOP-10 | resíduo legítimo não impede concluir | E13, E13b |

Os testes usam um `FakeExchange` com controlo de tempo: latência por tipo de chamada, ganchos antes/depois de cada chamada
(a flag liga-se exatamente entre duas chamadas), offline, falha persistente e fill durante o DELETE. Foram validados por
**mutação** (tirar as leituras do stop, dar STOPPED sem confirmar, vender o saldo todo da conta, alertas síncronos,
prazo que declara STOPPED, etc. fazem falhar pelo menos um teste).

## Limites conhecidos

- A janela entre a última leitura do stop e o envio HTTP existe (não há como a eliminar); está limitada a 1 chamada e
  fechada pela 3.ª leitura e pela reconciliação.
- O passo de 60 s e o vigilante de 5 s continuam a ser os que empurram a paragem; sem o corredor a correr nada avança
  (o estado STOPPING fica guardado e continua quando o corredor voltar).
- Uma ordem de emergência que a exchange recusa repetidamente tenta-se 3 vezes seguidas e depois uma por minuto, com o
  alerta CRÍTICO a cada prazo.
- Um bot parado com `residual` continua a ter essa moeda na conta (é dele); a fase seguinte decide o que fazer com ela.
- Mudar o prazo ainda não tem campo no painel (só na definição `emergency_deadline_s`).
