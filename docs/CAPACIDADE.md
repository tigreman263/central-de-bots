# Capacidade operacional

Responde a uma pergunta: **"este equipamento ainda tem margem para eu adicionar mais bots?"** Funciona igual em Raspberry
Pi, PC Windows ou Linux, mini-PC, servidor ou VPS. Não há regras por modelo nem um número de bots "máximo": mede o que o
sistema realmente sente, neste equipamento.

O monitor **só informa**. Nunca pára, pausa ou altera bots, e não toca em ordens, estratégia, recuperação nem no PARAR TUDO.
O estado aparece em **Início** (cartão pequeno) e em **Configuração → Sistema** (detalhe).

## Os quatro estados

| Estado | Significa | Mensagem |
|---|---|---|
| ● NORMAL | Há margem. | Nenhuma ação necessária. |
| ● ATENÇÃO | A carga está a subir. | A carga do equipamento está a aumentar. Monitorize a capacidade antes de adicionar mais bots. |
| ● ELEVADO | A capacidade começa a ficar comprometida. | Carga elevada. A execução de mais bots poderá afetar o desempenho. Considere migrar para um equipamento com maior capacidade. |
| ● CRÍTICO | Sinais de capacidade insuficiente. | Capacidade operacional comprometida. Evite adicionar novos bots e considere migrar para um equipamento com maior capacidade. |

Migrar quer dizer, por exemplo: Raspberry Pi → PC doméstico → servidor → VPS. Uma VPN serve para acesso remoto seguro e
**não** aumenta a capacidade de cálculo.

## O que se mede

O corredor faz **uma leitura por ciclo (60 s)**, sem processos novos e sem pedidos à Binance.

| Indicador | Origem | Peso na decisão |
|---|---|---|
| **Runner** (atraso) | duração do ciclo contra o intervalo esperado; atraso = duração − intervalo | sintoma do sistema (manda) |
| **Erros / timeouts** | pedidos à Binance/Testnet que falharam (429, 5xx, sem resposta) | sintoma do sistema (manda) |
| **Fila** | ordens por enviar ou reconciliar + cancelamentos na fila, de todos os bots | sintoma do sistema (manda) |
| Latência | tempo médio por pedido | recurso (limitado) |
| CPU | uso da máquina, média da janela | recurso (limitado) |
| RAM (sistema) | % em uso da máquina inteira | recurso (limite real, com a confirmação abaixo) |
| RAM (central) | RSS do próprio processo (corredor); MB, não % | confirma se a RAM do sistema é mesmo a app |
| Disco | % em uso; espaço livre | recurso (limite real) |
| Rede | mostra o pior de erros e latência (bytes/s só onde o sistema os dá) | derivado |
| Bots ativos, threads | contexto | não decide |

Sem medição possível (ex.: CPU num sistema sem contadores) o indicador aparece como `n/d` e não conta.
Um erro HTTP 4xx (por ex., "ordem inexistente") é uma resposta normal da exchange e **não** conta como falha.

## Como se calcula o estado

1. Cada indicador vira um nível (0 a 3) e uma severidade (0 a 100 %) por limiares genéricos (`capacity.TH`). **Não são
   limites universais**: servem só para o alarme dar sinal; a decisão é a combinação.
2. **Sintomas do sistema mandam.** CPU, latência e disco, sozinhos, nunca passam de ATENÇÃO enquanto o corredor está em
   dia e os pedidos não falham. Disco só ultrapassa isso perto de esgotar, que é um limite real: ≥ 93 % ou menos de
   0,5 GB livres.
3. **RAM: sistema vs. central.** A leitura do sistema operativo é de TODO o equipamento — Windows, browser, IDE,
   Claude Code, tudo. Por isso a RAM do sistema **só sobe o estado sozinha quando se confirma que é a própria app**:
   mede-se também a RAM só do processo do corredor (`ram_app`, em MB). Se essa medição existir e mostrar a app
   saudável (bem abaixo de 150 MB), a RAM do sistema fica só informativa — visível no painel, mas presa a NORMAL —
   mesmo que esteja a 93 % por causa de outros programas. Se a app está sob pressão (`ram_app` elevado) ou não há
   como medir o processo neste sistema operativo (fallback), a RAM do sistema volta a poder subir o estado sozinha,
   até 94 % (ELEVADO) ou 97 % (CRÍTICO). O `ram_app` em si também é um indicador real: se for a PRÓPRIA app a crescer
   muito (150/300/500 MB), isso nunca é escondido.
4. CPU baixa mas corredor atrasado e pedidos a falhar sobe na mesma.
5. Vários sinais em conjunto agravam a barra dentro da faixa; 3 ou mais indicadores na mesma faixa sobem ao estado seguinte.
6. A barra vai de 0 a 100 %: NORMAL < 25, ATENÇÃO < 50, ELEVADO < 75, CRÍTICO a partir de 75.
7. **Sem falsos alertas:** o corredor conta pela mediana dos últimos 3 ciclos (um ciclo lento isolado não conta; dois
   seguidos sim); a CPU e os erros olham para uma janela de 10 ciclos; **subir** exige 2 avaliações seguidas acima do estado
   atual, **descer** exige 5 seguidas abaixo (por degraus).

## Alertas

Usam o sistema de alertas que já existe (painel + Telegram/WhatsApp, conforme a gravidade mínima que escolheste). Só há
alerta quando o estado **muda**: subir dá "atenção" (ATENÇÃO) ou "alto" (ELEVADO, CRÍTICO); recuperar dá uma mensagem
informativa ("voltou a NORMAL"). Não se repete a cada ciclo, e um reinício não repete o último estado.

## Erros internos repetidos (backoff)

Um bug ou uma configuração impossível (por exemplo, um bot sem capital suficiente para montar a grelha do seu par)
pode fazer o mesmo passo falhar sempre. O 1.º erro fica sempre visível de imediato. Se o **mesmo** erro (mesmo sítio —
o passo ou o envio de ordens — e mesmo tipo) se repetir, o intervalo até ao próximo aviso cresce a cada repetição
(60 s, 240 s, 960 s, ... até 1 h): o bot continua a tentar todos os ciclos, na mesma, sem alterar nada da estratégia
ou das ordens — só o AVISO é que espaçado. Um erro **diferente** (tipo ou sítio diferentes) volta a ser imediato. Uma
vez que um passo corra bem, a folga apaga-se: a próxima falha, seja qual for, também é imediata. Isto nunca atrasa
nem bloqueia o PARAR TUDO, a reconciliação ou a recuperação, que não passam por este mecanismo.

## Vigilante do PARAR TUDO e SQLite

O vigilante (~5 s) abre e fecha uma ligação SQLite a cada volta, mesmo com a decisão de manter assim depois de medir:
uma ligação persistente pouparia microssegundos, mas arriscaria não ver uma base de dados restaurada de uma cópia de
segurança enquanto estivesse presa ao ficheiro antigo (o cenário de restauro documentado na Fase 1). O que se evita,
sim: com a flag desligada (o caso normal) já não se lê o ficheiro das chaves nem se constrói a ligação à Testnet —
só depois de confirmar que há mesmo um PARAR TUDO em curso.

## Histórico

Uma linha por **5 minutos** (`capacity_history`), 14 dias. Guarda médias e máximos de CPU e RAM, duração média e máxima dos
ciclos, atraso máximo, pedidos, erros, timeouts, latência, fila e bots. O painel resume as últimas 24 h. O estado atual
guarda-se numa só definição (`capacity_snapshot`), uma escrita pequena por ciclo, ao lado do sinal de vida do corredor.

## Limitações

- Os limiares são genéricos e conservadores; um equipamento pode viver bem acima de um deles. Vale a combinação e a
  tendência, não um valor isolado.
- A CPU medida é sempre da máquina inteira (não só do programa); outros programas no mesmo equipamento contam. A RAM
  já distingue sistema de app (`ram_app`) em Windows e Linux; noutros sistemas não há essa leitura e a RAM do sistema
  volta a poder decidir sozinha (fallback documentado, não escondido).
- Em Windows e Linux medem-se CPU, RAM (sistema e app), disco e threads sem dependências; a rede em bytes/s só em
  Linux. Noutros sistemas a CPU é uma aproximação pela carga média.
- O histórico de 5 minutos (`capacity_history`) ainda só guarda a RAM do sistema, não a da app; a tendência da RAM da
  app só está disponível no instante atual (`capacity_snapshot`), não numa série longa.
- A latência inclui a rede e a Binance, não só o equipamento.
- Só mede enquanto o corredor corre; se parar, o painel mostra "Sem dados recentes" em vez de um NORMAL antigo.
- O atraso mede o ciclo do corredor; não deteta um bot lento por dentro dele.
