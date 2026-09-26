# Malha (v0.3, Testnet)

**Malha**: central de bots de grelha para a Binance, com painel web.

Bots de grelha em **simulação** ou na **Binance Testnet** (ordens reais numa conta fictícia). Nunca opera na conta real:
a chave real é só de leitura. Ver `docs/DESIGN.md`, `docs/UI.md`, `docs/V02.md`, `docs/V03.md`.

## Correr no PC
Dois processos separados:
```
.venv\Scripts\python.exe run.py          # painel (servidor de desenvolvimento): http://127.0.0.1:5000
.venv\Scripts\python.exe run_bots.py     # corredor dos bots (+ vigilante do PARAR TUDO a cada ~5 s)
```
Na primeira vez o painel pede para criar a palavra-passe.

## Testnet e alertas
1. Painel > Bots > Ligações: cola as chaves da Testnet (testnet.binance.vision; não servem na conta real).
2. Painel > Configuração > Notificações: liga os alertas por Telegram e/ou WhatsApp (CallMeBot ou Cloud API da Meta), com
   passos explicados, mensagem de teste e a gravidade mínima por canal. Só enviam mensagens: não recebem comandos.
3. Bots > separador Testnet > Criar bot na Testnet: escolhes o par (só pares que existem na Testnet), vês a grelha e aprovas. Podes criar
   também um bot gémeo em simulação, com as mesmas velas, para comparar em Estatísticas.
4. Se a Testnet for reposta, o bot pára e avisa; crias um bot novo para recomeçar (a janela de 7 dias reinicia).

As chaves ficam em `~/.central-de-bots/` (fora do projeto, da base de dados e do git).

## Raspberry Pi 24/7
Instalação (Pi OS Bookworm, 64 bits): copia o projeto para `/opt/bots/app` e corre `sudo bash deploy/install_pi.sh`.
Cria o utilizador `bots` (sem shell), o ambiente Python, dois serviços systemd (`bots-painel` com waitress e
`bots-corredor`, ambos com reinício automático) e dois temporizadores: cópia de segurança horária da base de dados
(`app/backup.py`) e um vigia que avisa no Telegram se o corredor deixar de dar sinal (`app/watchdog.py`).

- Variáveis em `/etc/bots.env` (modelo em `deploy/bots.env.example`): `BOTS_DATA_DIR`, `BOTS_KEYS_DIR`, `BOTS_HOST`,
  `BOTS_PORT`, `BOTS_HTTPS`. Os dados e as chaves ficam fora do código.
- O painel escuta só em `127.0.0.1`. Acede por túnel SSH (`ssh -L 5000:127.0.0.1:5000 pi`) ou por VPN (WireGuard/Tailscale),
  nunca com a porta aberta no router. A palavra-passe só se cria a partir do próprio Pi (ou do túnel).
- Logs: `journalctl -u bots-corredor -f`. Só corre um corredor de cada vez (ficheiro de bloqueio).
- A base de dados usa WAL. Prefere um SSD USB ao cartão SD. As chaves não vão nas cópias: guarda-as à parte, cifradas.
- O relógio tem de estar certo (`chrony`, instalado pelo script); o corredor também compensa pequenas diferenças.
- Ainda não foi testado num Pi a sério: faz primeiro o checklist do primeiro dia (docs/V03.md).

## Painel
- **Bots** em três separadores: Real (ainda não existe: só simulação e Testnet), Simulação e Testnet. Um bot parado apaga-se
  com um botão (com confirmação), um a um ou todos os parados do separador.
- **Configuração** por categorias: Capital e saldo, Risco e lucro, Alertas de risco da carteira, Sugestões de moedas,
  Notificações, Ligações e Sistema.

## Regras dos bots
- "Nunca vender abaixo do custo" (opcional por bot, desligada por defeito): o stop-loss pausa em vez de vender com
  prejuízo e o "parar" mantém a moeda.
- O painel mostra por bot: realizado vs por realizar, comparação com comprar e manter, ciclos por dia, comissões
  sobre o ganho bruto, queda máxima do capital e distância ao stop.

## Testes
```
.venv\Scripts\python.exe -m pytest -q
```
Os testes da Testnet usam uma exchange falsa em memória: nenhum fala com a Binance nem lê as chaves reais.

Os dados ficam em `data/` (base de dados e chave da sessão). Não vão para o git.
