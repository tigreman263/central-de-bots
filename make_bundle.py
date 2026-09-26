"""Junta o projeto num só ficheiro Markdown para ser analisado por outra IA (por exemplo o ChatGPT)."""
from pathlib import Path

root = Path(__file__).resolve().parent
out = root / "analise-chatgpt.md"

intro = '''# Pedido de análise: Malha (bots de grelha para a Binance)

És um revisor sénior de software e de sistemas de trading automático. Analisa este projeto com espírito crítico e responde
em português de Portugal. Sê concreto: cada achado com ficheiro e função, o cenário em que falha, a gravidade
(Alta/Média/Baixa) e a correção proposta. Não elogies por elogiar. Diz também o que NÃO consegues avaliar só a ler o código.

## Contexto
- Projeto pessoal de um utilizador não técnico, escrito com ajuda de IA. Python 3.12, Flask, SQLite, sem dependências pesadas.
- Objetivo: painel web + bots de grelha (grid trading, spot, 1 par, sem alavancagem) para a Binance, a correr num Raspberry Pi 24/7.
- Estado: v0.3. Bots em simulação (paper) e na Binance TESTNET (ordens reais numa conta fictícia). A conta real é só de LEITURA.
- Capital de referência: ~77 USDT por bot (o utilizador tem ~514 EUR no total). Objetivo declarado: gerar lucro líquido de comissões
  e reforçar uma reserva, aprendendo pelo caminho.

## Regras firmes do utilizador (não as contestes, mas diz se o código as viola)
1. A IA nunca envia ordens. Só `app/trader.py` envia/cancela ordens, e só para https://testnet.binance.vision.
2. Todas as ordens passam por `grid.validate_order` (guarda de risco) antes de sair.
3. A chave real da Binance é só de leitura e nunca chega ao módulo das ordens.
4. Retomar sem duplicar ordens depois de um corte de luz/processo (estado gravado antes do envio, ids únicos, reconciliação com a exchange).
5. Lucro sempre líquido de comissões; PARAR TUDO tem de cancelar tudo em segundos.
6. Decisões pedidas uma a uma; nada de dinheiro real nesta fase.

## Arquitetura em 30 segundos
- `app/engine.py`: motor da grelha. Só decide; não fala com a rede. Modo `sim` (simulador) ou `testnet` (recebe execuções reais).
- `app/executor.py`: traduz as decisões em ordens reais, reconcilia com a exchange (fonte da verdade), retoma após cortes.
- `app/trader.py`: único módulo com POST/DELETE, só Testnet, sem ler ficheiros de chaves.
- `app/runner.py` + `run_bots.py`: corredor (passo de 60 s + vigilante do PARAR TUDO a cada ~5 s, duas threads, um lock).
- `app/botstore.py`, `app/db.py`: SQLite (WAL). `app/grid.py`: matemática da grelha e guarda de risco. `app/chart.py`: gráfico SVG.
- `app/__init__.py`: painel Flask (login, CSRF, CSP, páginas). `app/notify.py`: alertas Telegram (só envio).
- `deploy/`: serviços systemd, instalação no Pi, backup, vigia do corredor.

## Estado dos testes (honesto)
- 153 testes passam, mas TODOS contra uma exchange falsa em memória (`tests/test_testnet.py::FakeExchange`) ou mercados sintéticos.
- Nunca correu contra a Testnet real (o formato exato das respostas de envio/cancelamento e os códigos de erro estão assumidos).
- Nunca correu num Raspberry Pi real. O lucro esperado com este capital é irrisório (~0,1 a 0,4 USDT/dia, só em mercado lateral).
- Já houve uma revisão interna por 7 agentes; os achados principais foram corrigidos (ver `docs/V03.md`).

## O que quero de ti
1. Bugs de correção e casos-limite no motor, no executor e no corredor (corridas entre threads, estados inconsistentes após cortes,
   execuções parciais, comissões, pó, reset da Testnet, recentragem, ids de ordens).
2. Testes fracos ou circulares (asserções que passam por razões erradas) e o que a `FakeExchange` não simula mas devia.
3. Riscos de segurança (painel, chaves, sessões, Telegram, Pi) e se as regras firmes podem ser contornadas.
4. A estratégia: parâmetros por defeito coerentes? Onde a grelha perde sistematicamente? O que falta (filtro de tendência,
   degraus por volatilidade, post-only, comissão em BNB)? Como validar honestamente (backtest, nº de ciclos, comparação com buy&hold)?
5. Diferenças Testnet vs mercado real que enganam os resultados.
6. Operação no Pi: o que falha em 24/7 (SD, relógio, energia, logs, backups).
7. As 10 melhorias que mais reduzem risco, por ordem de prioridade, e o que deves perguntar ao utilizador antes.

Abaixo seguem a documentação e o código-fonte (sem os templates HTML/CSS, que são só apresentação).

'''

docs = ["README.md", "docs/DESIGN.md", "docs/V02.md", "docs/V03.md", "docs/PORTFOLIO.md", "docs/PORTFOLIO2.md", "docs/UI.md"]
code = ["app/engine.py", "app/executor.py", "app/trader.py", "app/runner.py", "run_bots.py", "app/grid.py",
        "app/botstore.py", "app/db.py", "app/notify.py", "app/chart.py", "app/pairs.py", "app/keystore.py", "app/config.py",
        "app/lock.py", "app/log.py", "app/backup.py", "app/watchdog.py", "app/reader.py", "app/__init__.py", "run.py", "wsgi.py",
        "app/alerts.py", "app/risk.py", "app/rules.py", "app/portfolio.py", "app/costbasis.py", "app/staking.py",
        "app/analysis.py", "app/layout.py", "app/market.py", "app/recommendations.py", "app/explain.py"]
tests = ["tests/conftest.py", "tests/test_testnet.py", "tests/test_v03_fixes.py", "tests/test_grid.py", "tests/test_bots.py"]
deploy = sorted(str(p.relative_to(root)).replace("\\", "/") for p in (root / "deploy").glob("*"))


def block(rel):
    text = (root / rel).read_text(encoding="utf-8")
    lang = {"py": "python", "md": "markdown", "sh": "bash"}.get(rel.rsplit(".", 1)[-1], "")
    return f"\n\n## Ficheiro: {rel}\n\n```{lang}\n{text}\n```\n"


parts = [intro, "# PARTE 1: documentação\n"]
parts += [block(r) for r in docs]
parts += ["\n\n# PARTE 2: código da aplicação\n"] + [block(r) for r in code]
parts += ["\n\n# PARTE 3: testes\n"] + [block(r) for r in tests]
parts += ["\n\n# PARTE 4: instalação no Pi\n"] + [block(r) for r in deploy]
out.write_text("".join(parts), encoding="utf-8")
print(out, round(out.stat().st_size / 1024), "KB", sum(len(p) for p in parts) // 4, "tokens aprox.")
