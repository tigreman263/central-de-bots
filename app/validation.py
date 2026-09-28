"""Validação do projeto: uma checklist com evidência real (testes existentes, ficheiros do repositório, ou uma
admissão explícita do que ainda não foi verificado), guardada e mostrada num painel visual.

Regra de fundo: nunca inventar um resultado. Um item só é PASSOU se um teste real passou agora mesmo, ou se um
ficheiro que tem de existir existe. Tudo o que não pode ser confirmado por código fica NÃO_VALIDADO, com o motivo
escrito — mesmo que pareça óbvio que funciona (ex.: nunca correu num Raspberry Pi real).
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import db
from .log import log

ROOT = Path(__file__).resolve().parent.parent

PASSOU, FALHOU, PARCIAL, NAO_VALIDADO, NA = "passou", "falhou", "parcial", "nao_validado", "na"
STATES = [PASSOU, FALHOU, PARCIAL, NAO_VALIDADO, NA]
LABELS = {PASSOU: "Passou", FALHOU: "Falhou", PARCIAL: "Parcial", NAO_VALIDADO: "Não validado", NA: "N/A"}
ICONS = {PASSOU: "✅", FALHOU: "❌", PARCIAL: "⚠️", NAO_VALIDADO: "❓", NA: "➖"}
PILL = {PASSOU: "gain", FALHOU: "loss", PARCIAL: "warn", NAO_VALIDADO: "idle", NA: "idle"}
_RANK = {FALHOU: 4, PARCIAL: 3, NAO_VALIDADO: 2, PASSOU: 1, NA: 0}   # para escolher o "peor" resultado de vários testes


def T(file, name):
    return f"tests/{file}.py::{name}"


# ---------- a checklist ----------
# how: "pytest" (tests: lista de node ids; passa se TODOS passarem) | "file" (paths: lista; têm de existir) |
#      "manual" (status fixo + evidência escrita à mão, nunca "passou" por inferência).
# critical: se este item, quando não está PASSOU, bloqueia a auditoria final.
CHECKLIST = [
    # ---------------- 1. Roadmap / Funcionalidades ----------------
    {"id": "rm-painel-login", "cat": "Roadmap / Funcionalidades", "name": "Painel com login e palavra-passe",
     "requirement": "Só se vê alguma coisa depois de definir e usar uma palavra-passe; bloqueia após 5 erros.",
     "how": "pytest", "tests": [T("test_v01", "test_nothing_visible_without_password"),
                                T("test_v01", "test_first_use_forces_password_creation"),
                                T("test_v01", "test_wrong_password_rejected_and_locked_after_five")],
     "critical": True, "impact": "Sem isto qualquer pessoa na rede local via o painel."},
    {"id": "rm-simulacao", "cat": "Roadmap / Funcionalidades", "name": "Bot de grelha em simulação (v0.2)",
     "requirement": "Simulador de grelha com proteções (stop, pausa, recentragem) e contabilidade líquida de comissões.",
     "how": "pytest", "tests": [T("test_grid", "test_grid_is_built_with_reserve_and_within_limits"),
                                T("test_grid", "test_profit_is_net_of_fees_and_accounting_is_consistent")],
     "critical": True, "impact": "Base de todos os bots; sem isto não há estratégia nenhuma a correr."},
    {"id": "rm-testnet", "cat": "Roadmap / Funcionalidades", "name": "Bot na Binance Testnet (v0.3)",
     "requirement": "Ordens reais (dinheiro fictício) enviadas e reconciliadas com a Testnet.",
     "how": "pytest", "tests": [T("test_testnet", "test_bot_places_real_orders_with_unique_ids_and_valid_filters")],
     "critical": True, "impact": "É o modo de teste antes de dinheiro real."},
    {"id": "rm-gemeo", "cat": "Roadmap / Funcionalidades", "name": "Bot gémeo (simulação + Testnet no mesmo par)",
     "requirement": "Criar um bot cria também o duplicado no outro modo, com as mesmas velas.",
     "how": "pytest", "tests": [T("test_testnet", "test_sim_twin_reads_testnet_candles_and_sends_no_orders"),
                                T("test_config_alerts", "test_creating_in_simulation_can_also_create_the_duplicate_on_the_testnet")],
     "critical": False, "impact": "Só afeta a comparação simulação vs. Testnet."},
    {"id": "rm-abas", "cat": "Roadmap / Funcionalidades", "name": "Bots em separadores (Real/Simulação/Testnet)",
     "requirement": "A lista de bots organiza-se por tipo, cada aba com contagem.",
     "how": "pytest", "tests": [T("test_config_alerts", "test_bots_are_split_into_real_simulation_and_testnet_tabs")],
     "critical": False, "impact": "Só organização visual."},
    {"id": "rm-apagar", "cat": "Roadmap / Funcionalidades", "name": "Apagar bots parados",
     "requirement": "Só um bot parado e sem nada pendente na exchange pode ser apagado; o registo de trades fica.",
     "how": "pytest", "tests": [T("test_config_alerts", "test_only_stopped_bots_without_pending_work_can_be_deleted"),
                                T("test_recovery", "test_a_fresh_database_needs_no_backup_and_deleting_a_bot_keeps_its_trade_registry")],
     "critical": False, "impact": "Apagar um bot com trabalho pendente perderia rasto na exchange."},
    {"id": "rm-config-cat", "cat": "Roadmap / Funcionalidades", "name": "Configuração dividida por categorias",
     "requirement": "Capital, risco, alertas, ligações, pares, sistema — cada um na sua secção, com recomendações.",
     "how": "pytest", "tests": [T("test_config_alerts", "test_configuration_is_split_into_categories_with_a_working_default"),
                                T("test_v01", "test_config_shows_recommendations_for_every_field")],
     "critical": False, "impact": "Configuração difícil de navegar sem isto."},
    {"id": "rm-alertas-canais", "cat": "Roadmap / Funcionalidades", "name": "Alertas por Telegram e WhatsApp",
     "requirement": "Dois canais configuráveis, testados antes de gravar, com gravidade mínima por canal.",
     "how": "pytest", "tests": [T("test_config_alerts", "test_alert_preferences_are_saved_and_default_to_telegram_attention_whatsapp_high"),
                                T("test_config_alerts", "test_whatsapp_is_tested_before_saving_and_secrets_are_never_shown")],
     "critical": False, "impact": "Sem alertas remotos, só o painel avisa."},
    {"id": "rm-pares-config", "cat": "Roadmap / Funcionalidades", "name": "Critérios de sugestão de pares configuráveis",
     "requirement": "Volume, spread e amplitude configuráveis; alarga sozinho se nada cumprir, e diz que o fez.",
     "how": "pytest", "tests": [T("test_pair_settings", "test_changing_a_criterion_changes_the_result"),
                                T("test_pair_settings", "test_when_nothing_meets_the_criteria_they_are_widened_in_steps_and_it_says_so")],
     "critical": False, "impact": "Sem isto, os critérios fixos podiam deixar de ter pares se o mercado mudasse muito."},
    {"id": "rm-ativar", "cat": "Roadmap / Funcionalidades", "name": "Botão Ativar individual por bot",
     "requirement": "Depois do PARAR TUDO, cada bot tem o seu Ativar; nunca existe Ativar Todos.",
     "how": "pytest", "tests": [T("test_activate", "test_after_stop_all_every_bot_keeps_its_own_activate_button_and_there_is_no_activate_all"),
                                T("test_activate", "test_there_is_no_activate_all_endpoint_or_action_anywhere")],
     "critical": True, "impact": "Sem isto, reativar bots ao acaso podia duplicar ordens."},
    {"id": "rm-ai", "cat": "Roadmap / Funcionalidades", "name": "Área Ai (resumo + link configurável, sem chave)",
     "requirement": "Resumo sem segredos, copiável, e link da IA configurável para abrir e colar.",
     "how": "pytest", "tests": [T("test_portfolio2", "test_ai_summary_json_carries_the_cost_and_unrealized_pnl_not_just_the_page"),
                                T("test_portfolio2", "test_the_ai_link_can_be_configured_changed_and_removed"),
                                T("test_portfolio2", "test_ai_copy_button_script_has_the_csp_nonce_and_no_network_call")],
     "critical": False, "impact": "Funcionalidade de apoio; não afeta trading."},
    {"id": "rm-capacidade", "cat": "Roadmap / Funcionalidades", "name": "Monitor de capacidade operacional",
     "requirement": "Estado NORMAL/ATENÇÃO/ELEVADO/CRÍTICO visível no painel, calculado a partir de métricas reais.",
     "how": "pytest", "tests": [T("test_capacity", "test_home_card_and_system_tab_show_capacity_in_the_existing_style")],
     "critical": False, "impact": "Só informa; nunca decide nada dos bots."},

    # ---------------- 2. Trading e Ordens ----------------
    {"id": "tr-guarda", "cat": "Trading e Ordens", "name": "Guarda de risco recusa ordens fora dos filtros",
     "requirement": "Nenhuma ordem sai sem passar por `validate_order` (passo, preço, mínimo, limites).",
     "how": "pytest", "tests": [T("test_grid", "test_guard_refuses_bad_configurations"), T("test_testnet", "test_guard_refuses_bad_orders_before_they_leave")],
     "critical": True, "impact": "Sem isto podiam sair ordens que a exchange recusaria ou piores."},
    {"id": "tr-ids-unicos", "cat": "Trading e Ordens", "name": "Ids de ordem únicos, mesmo após recriar a base de dados",
     "requirement": "O id do bot inclui um identificador aleatório; nunca colide entre bots ou reinstalações.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_ids_never_collide_after_recreating_the_database")],
     "critical": True, "impact": "Uma colisão de ids podia fazer o sistema adotar a ordem errada."},
    {"id": "tr-retoma", "cat": "Trading e Ordens", "name": "Retoma sem duplicar após crash (5 pontos de corte)",
     "requirement": "O processo morto a meio do envio, do fill ou do cancelamento nunca duplica nem perde nada.",
     "how": "pytest", "tests": [T("test_testnet", n) for n in (
         "test_crash_1_after_saving_orders_before_sending_them", "test_crash_2_order_reached_the_exchange_but_the_answer_was_lost",
         "test_crash_3_after_partial_fill_before_saving_counts_it_once", "test_crash_4_after_full_fill_before_saving_counts_it_once",
         "test_crash_5_cancel_done_on_the_exchange_but_not_recorded")] + [T("test_v03_fixes", "test_crash_6_in_the_middle_of_sending_the_orders")],
     "critical": True, "impact": "É o requisito mais antigo do projeto: nunca duplicar."},
    {"id": "tr-comissoes", "cat": "Trading e Ordens", "name": "Comissões reais aplicadas (moeda, USDT ou BNB)",
     "requirement": "A contabilidade usa a comissão que a exchange cobrou de facto, não uma taxa fixa.",
     "how": "pytest", "tests": [T("test_testnet", "test_cycle_uses_real_prices_and_real_commissions"),
                                T("test_v03_fixes", "test_commission_paid_in_another_coin_is_not_taken_from_the_balance")],
     "critical": True, "impact": "Contabilidade errada se a comissão não for a real."},
    {"id": "tr-nunca-abaixo-custo", "cat": "Trading e Ordens", "name": "Regra opcional 'nunca vender abaixo do custo'",
     "requirement": "Se ligada, cancela e mantém a moeda em vez de vender com prejuízo.",
     "how": "pytest", "tests": [T("test_v03_fixes", n) for n in (
         "test_never_sell_below_cost_keeps_the_coin_on_a_manual_stop", "test_never_sell_below_cost_still_sells_when_the_position_is_in_profit",
         "test_automatic_stop_loss_pauses_instead_of_selling_below_cost")],
     "critical": False, "impact": "Só afeta bots que ligarem esta opção (desligada por defeito)."},
    {"id": "tr-passo-nao-encolhe", "cat": "Trading e Ordens", "name": "A quantidade do degrau não encolhe ao longo dos ciclos",
     "requirement": "A comissão em moeda não vai reduzindo a quantidade planeada de cada degrau.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_step_quantity_does_not_erode_over_many_cycles")],
     "critical": False, "impact": "Sem isto, cada ciclo negociaria um pouco menos."},

    # ---------------- 3. Restart ----------------
    {"id": "re-simulacao", "cat": "Restart", "name": "Restart em simulação retoma sem duplicar",
     "requirement": "Reiniciar o processo com um bot em simulação continua exatamente de onde ficou.",
     "how": "pytest", "tests": [T("test_grid", "test_restart_resumes_exactly_without_duplicating_orders"),
                                T("test_bots", "test_runner_restart_resumes_without_duplicates_and_matches_a_continuous_run")],
     "critical": True, "impact": "Sem isto, cada reinício arriscava duplicar posição."},
    {"id": "re-fills-parciais", "cat": "Restart", "name": "Restart entre fills parciais não duplica nem perde",
     "requirement": "Fechar o processo entre dois fills da mesma ordem conta cada trade uma só vez.",
     "how": "pytest", "tests": [T("test_recovery", "test_restart_between_fills_never_duplicates")],
     "critical": True, "impact": "Fase 1: é o cenário mais comum num Raspberry Pi que reinicia."},
    {"id": "re-stopping", "cat": "Restart", "name": "Restart durante STOPPING não volta a RUNNING",
     "requirement": "Um bot a meio da paragem continua a parar depois de reiniciar; nunca volta a trabalhar sozinho.",
     "how": "pytest", "tests": [T("test_emergency", "test_e12_restart_during_stopping_continues_the_stop_and_never_goes_back_to_running")],
     "critical": True, "impact": "Voltar a RUNNING sozinho seria contrariar o PARAR TUDO."},
    {"id": "re-config", "cat": "Restart", "name": "Configuração sobrevive a um restart do painel",
     "requirement": "Os valores guardados continuam lá depois de o processo do painel reiniciar.",
     "how": "pytest", "tests": [T("test_v01", "test_config_survives_restart")],
     "critical": False, "impact": "Sem isto, a configuração perdia-se a cada reinício do painel."},
    {"id": "re-ativar-nao-auto", "cat": "Restart", "name": "Reiniciar o corredor não reativa bots sozinho",
     "requirement": "Depois do PARAR TUDO, um restart do corredor não faz nenhum bot arrancar por si.",
     "how": "pytest", "tests": [T("test_activate", "test_restarting_the_runner_after_stop_all_never_reactivates_bots_automatically")],
     "critical": True, "impact": "Reativação automática seria o oposto do que o PARAR TUDO garante."},

    # ---------------- 4. Recovery / Reconciliação ----------------
    {"id": "rec-registo-idempotente", "cat": "Recovery / Reconciliação", "name": "Cada trade contabilizado uma só vez (I3)",
     "requirement": "O registo de trades tem chave única; o mesmo trade nunca é contado duas vezes.",
     "how": "pytest", "tests": [T("test_recovery", "test_the_same_trade_is_never_booked_twice")], "critical": True,
     "impact": "Contar um trade a mais falsifica a posição e o lucro."},
    {"id": "rec-fills-incrementais", "cat": "Recovery / Reconciliação", "name": "Fills parciais incrementais (30/50/100%)",
     "requirement": "Dinheiro, moeda e comissões contam-se trade a trade; o degrau só muda quando a ordem termina.",
     "how": "pytest", "tests": [T("test_recovery", "test_partial_fills_30_50_100_are_booked_incrementally_and_only_once"),
                                T("test_recovery", "test_partial_sell_fills_are_booked_incrementally_and_the_cycle_closes_once")],
     "critical": True, "impact": "Bug confirmado antes da Fase 1: fills parciais só contavam no fim."},
    {"id": "rec-mytrades-incompleto", "cat": "Recovery / Reconciliação", "name": "myTrades incompleto nunca é disfarçado",
     "requirement": "Se a soma dos trades não bate com o executado, a ordem fica RECONCILIATION_PENDING até corrigir.",
     "how": "pytest", "tests": [T("test_recovery", "test_incomplete_my_trades_0_078_vs_0_023377_is_flagged_and_corrected_later"),
                                T("test_recovery", "test_delayed_trades_1_then_2_then_3_keep_the_order_pending_until_complete"),
                                T("test_recovery", "test_trades_that_never_show_up_are_adjusted_explicitly_after_six_hours")],
     "critical": True, "impact": "Bug confirmado: executedQty=0,078 com myTrades só devolvendo 0,023377."},
    {"id": "rec-reconcile-sempre", "cat": "Recovery / Reconciliação", "name": "Reconcile corre mesmo com 0 ordens locais",
     "requirement": "Nunca se salta a reconciliação só porque a base local não tem ordens guardadas.",
     "how": "pytest", "tests": [T("test_recovery", "test_zero_local_orders_and_six_on_the_exchange_recovers_before_creating_any")],
     "critical": True, "impact": "Bug confirmado (executor.py:99): local=0 fazia criar ordens a mais na exchange."},
    {"id": "rec-reset-ordens", "cat": "Recovery / Reconciliação", "name": "Reset com ordens abertas reconhece as restantes",
     "requirement": "Uma ordem que desaparece não faz esquecer as outras que continuam vivas na exchange.",
     "how": "pytest", "tests": [T("test_recovery", "test_reset_with_open_orders_recognises_the_rest_and_never_stops"),
                                T("test_recovery", "test_the_real_testnet_reset_still_stops_and_keeps_all_history")],
     "critical": True, "impact": "Cenário perigoso confirmado: 6 ordens, 1 desaparece, as outras ficavam por reconhecer."},
    {"id": "rec-saldo-2-sentidos", "cat": "Recovery / Reconciliação", "name": "Saldo verificado nos dois sentidos (I8)",
     "requirement": "Local igual, menor e MAIOR do que a exchange são todos detetados; nunca só 'menor'.",
     "how": "pytest", "tests": [T("test_recovery", n) for n in (
         "test_balance_equal_is_a_match_and_stays_quiet", "test_balance_local_greater_than_exchange_is_reported_without_a_reset",
         "test_balance_much_lower_than_local_is_a_reset_not_a_loss", "test_balance_exchange_greater_than_local_is_detected_and_never_sold")],
     "critical": True, "impact": "Antes só se via saldo menor; saldo maior nunca gerava aviso (falso negativo)."},
    {"id": "rec-restauro-backup", "cat": "Recovery / Reconciliação", "name": "Restauro de uma cópia de segurança antiga",
     "requirement": "Ordens de antes da cópia continuam reconhecidas depois de restaurar; nada duplica.",
     "how": "pytest", "tests": [T("test_recovery", "test_restoring_an_old_backup_recognises_old_orders_and_does_not_duplicate"),
                                T("test_recovery", "test_restore_of_a_backup_from_before_the_first_step_keeps_the_uid")],
     "critical": True, "impact": "Um restauro mal reconhecido duplicava ordens ou perdia posição."},
    {"id": "rec-residual", "cat": "Recovery / Reconciliação", "name": "Pó/residual nunca conta como perda realizada (I7)",
     "requirement": "Moeda abaixo do mínimo negociável fica representada, com custo, e não falseia o resultado do ciclo.",
     "how": "pytest", "tests": [T("test_recovery", "test_partial_sale_under_the_minimum_leaves_explicit_residual_without_a_false_loss"),
                                T("test_recovery", "test_residual_survives_a_recenter_and_is_sold_with_its_cost_on_liquidation")],
     "critical": True, "impact": "Bug confirmado: o custo do pó era subtraído ao lucro como se fosse perda."},
    {"id": "rec-migracao", "cat": "Recovery / Reconciliação", "name": "Migração de uma base de dados existente com cópia de segurança automática",
     "requirement": "Ao atualizar uma instalação com bots já criados, faz-se backup antes de alterar o esquema.",
     "how": "pytest", "tests": [T("test_recovery", "test_migration_of_an_existing_database_keeps_data_and_backs_it_up_first")],
     "critical": True, "impact": "Sem backup automático, uma migração falhada podia perder dados reais."},

    # ---------------- 5. PARAR TUDO ----------------
    {"id": "pt-stopping", "cat": "PARAR TUDO", "name": "Estado STOPPING distinto de STOPPED",
     "requirement": "PARAR TUDO nunca salta direto para parado; passa sempre por um estado intermédio confirmado.",
     "how": "pytest", "tests": [T("test_emergency", "test_stopping_has_labels_and_a_default_deadline"),
                                T("test_emergency", "test_e8_cancel_failure_keeps_stopping_and_never_declares_stopped")],
     "critical": True, "impact": "Declarar 'parado' sem confirmar é o risco central desta fase."},
    {"id": "pt-stop1", "cat": "PARAR TUDO", "name": "STOP-1: nenhuma ordem nova depois de observado o stop",
     "requirement": "Antes de cada POST, o pedido de paragem é lido de novo; nada de estratégia sai depois disso.",
     "how": "pytest", "tests": [T("test_emergency", n) for n in (
         "test_e1_stop_before_the_first_post_sends_nothing", "test_e2_stop_between_two_posts_blocks_every_later_post",
         "test_e3_stop_after_the_first_post_leaves_only_that_order", "test_e4_stop_during_a_post_marks_the_order_in_flight_and_cancels_it",
         "test_e18_stop_switched_on_during_a_tick_blocks_the_orders_that_tick_was_about_to_send")],
     "critical": True, "impact": "É o requisito mais importante do pedido original da Fase 2."},
    {"id": "pt-stop2", "cat": "PARAR TUDO", "name": "STOP-2: sem STOPPED com ordens abertas do bot",
     "requirement": "Só se confirma parado depois de a exchange não ter nenhuma ordem do bot.",
     "how": "pytest", "tests": [T("test_emergency", "test_e8_cancel_failure_keeps_stopping_and_never_declares_stopped"),
                                T("test_emergency", "test_e9_binance_offline_keeps_stopping_blocks_orders_and_finishes_when_back")],
     "critical": True, "impact": "Declarar parado com ordens vivas seria uma mentira operacional."},
    {"id": "pt-stop4", "cat": "PARAR TUDO", "name": "STOP-4: nunca vender posição desconhecida",
     "requirement": "Moeda na conta que nenhum bot explica não é vendida ao parar; só gera alerta.",
     "how": "pytest", "tests": [T("test_emergency", "test_e14_unknown_position_is_never_sold"),
                                T("test_emergency", "test_e14b_bot_without_position_and_coin_on_the_exchange_sells_nothing")],
     "critical": True, "impact": "Vender algo que pode não ser do bot seria um erro grave e irreversível."},
    {"id": "pt-stop5", "cat": "PARAR TUDO", "name": "STOP-5: fills durante o cancelamento contados uma vez",
     "requirement": "Uma ordem que executa durante o próprio cancelamento é contabilizada, não ignorada.",
     "how": "pytest", "tests": [T("test_emergency", n) for n in (
         "test_e6_cancel_answered_unknown_order_because_it_was_filled_is_booked_not_assumed_cancelled",
         "test_e6b_unknown_order_on_cancel_looks_the_order_up_and_books_the_fill_at_once",
         "test_e7_partial_fill_then_cancel_books_the_part_and_sells_only_what_exists")],
     "critical": True, "impact": "Ignorar um fill durante o cancelamento perderia moeda ou dinheiro da contabilidade."},
    {"id": "pt-stop6-7", "cat": "PARAR TUDO", "name": "STOP-6/7: falha ou Binance offline mantêm STOPPING",
     "requirement": "Sem ligação, ou com um cancelamento a falhar, o bot fica a tentar — nunca finge estar parado.",
     "how": "pytest", "tests": [T("test_emergency", "test_e8_cancel_failure_keeps_stopping_and_never_declares_stopped"),
                                T("test_emergency", "test_e9_binance_offline_keeps_stopping_blocks_orders_and_finishes_when_back")],
     "critical": True, "impact": "Um falso 'parado' sem rede escondia posição ainda aberta."},
    {"id": "pt-stop9", "cat": "PARAR TUDO", "name": "STOP-9: alertas não bloqueiam a paragem",
     "requirement": "Um Telegram lento não atrasa os outros bots a parar; envio em fila, fora do caminho crítico.",
     "how": "pytest", "tests": [T("test_emergency", "test_e10_slow_telegram_does_not_block_the_stop"),
                                T("test_emergency", "test_e11b_one_bot_talking_slowly_does_not_hold_the_others_hostage")],
     "critical": True, "impact": "Antes da Fase 2, um canal lento atrasava toda a paragem geral."},
    {"id": "pt-stop10", "cat": "PARAR TUDO", "name": "STOP-10: pó/residual não impede concluir a paragem",
     "requirement": "Não se tenta vender pó para sempre; a paragem termina como STOPPED_WITH_RESIDUAL.",
     "how": "pytest", "tests": [T("test_emergency", "test_e13_residual_below_the_minimum_ends_stopped_with_residual_and_keeps_the_cost")],
     "critical": True, "impact": "Sem isto, um bot com pó nunca terminaria a paragem."},
    {"id": "pt-concorrencia", "cat": "PARAR TUDO", "name": "tick_all e emergency_sweep nunca colidem no mesmo bot",
     "requirement": "Um fecho por bot garante que as duas passagens nunca alteram o mesmo bot ao mesmo tempo.",
     "how": "pytest", "tests": [T("test_emergency", f"test_tick_and_emergency_sweep_at_the_same_time_leave_one_consistent_state[{i}]") for i in range(4)],
     "critical": True, "impact": "Uma corrida aqui podia gravar um estado a meio, incoerente."},
    {"id": "pt-recovery-junto", "cat": "PARAR TUDO", "name": "PARAR TUDO durante RECOVERING não cria ordens novas",
     "requirement": "As duas fases seguras (recuperação e paragem) combinam-se sem nunca abrir uma grelha nova.",
     "how": "pytest", "tests": [T("test_emergency", "test_e15_recovery_and_emergency_stop_together_create_no_orders_and_end_stopped")],
     "critical": True, "impact": "Combinação de dois estados de segurança tinha de ser verificada junta, não só isolada."},

    # ---------------- 6. Alertas ----------------
    {"id": "al-canais", "cat": "Alertas", "name": "Telegram e WhatsApp testados antes de gravar a chave",
     "requirement": "Uma credencial só se guarda depois de um envio de teste ter mesmo funcionado.",
     "how": "pytest", "tests": [T("test_testnet", "test_panel_saves_testnet_keys_only_after_a_working_test"),
                                T("test_config_alerts", "test_whatsapp_is_tested_before_saving_and_secrets_are_never_shown")],
     "critical": False, "impact": "Sem teste prévio, uma chave errada só se descobria mais tarde."},
    {"id": "al-sem-segredo", "cat": "Alertas", "name": "A chave nunca aparece nas mensagens nem no painel",
     "requirement": "Nenhum alerta, log ou página mostra a chave completa, só uma versão parcial.",
     "how": "pytest", "tests": [T("test_config_alerts", "test_whatsapp_is_tested_before_saving_and_secrets_are_never_shown"),
                                T("test_portfolio", "test_key_never_appears_in_db_pages_or_logs")],
     "critical": True, "impact": "Uma fuga de chave é um risco de segurança direto."},
    {"id": "al-gravidade", "cat": "Alertas", "name": "Gravidade mínima por canal, sem repetir o mesmo alerta",
     "requirement": "Cada canal só recebe a partir da gravidade escolhida; o mesmo alerta não se repete em 24 h.",
     "how": "pytest", "tests": [T("test_config_alerts", "test_broadcast_sends_by_channel_according_to_severity_and_switches"),
                                T("test_portfolio", "test_same_alert_not_repeated_within_24h_and_repeats_after")],
     "critical": False, "impact": "Sem isto, cada aviso pequeno chegava a todos os canais sempre."},
    {"id": "al-canal-falha", "cat": "Alertas", "name": "Um canal a falhar não impede os outros nem o bot",
     "requirement": "Se o Telegram ou o WhatsApp falharem, o resto do sistema continua normalmente.",
     "how": "pytest", "tests": [T("test_config_alerts", "test_a_failing_channel_never_stops_the_others_or_the_bot")],
     "critical": True, "impact": "Um canal de alerta nunca deve poder travar a operação real."},
    {"id": "al-capacidade", "cat": "Alertas", "name": "Alertas de capacidade só na mudança de estado",
     "requirement": "O monitor de capacidade avisa ao subir e ao recuperar, nunca a cada ciclo com o mesmo estado.",
     "how": "pytest", "tests": [T("test_capacity", "test_alerts_only_on_state_changes_and_with_a_recovery_message")],
     "critical": False, "impact": "Sem isto, o monitor inundava os canais a cada minuto."},
    {"id": "al-backoff", "cat": "Alertas", "name": "Erros internos repetidos entram em backoff (não inundam)",
     "requirement": "O 1.º erro é imediato; o mesmo erro repetido espaça-se; um erro diferente é sempre imediato.",
     "how": "pytest", "tests": [T("test_capacity_fixes", n) for n in (
         "test_first_internal_error_is_reported_immediately", "test_the_same_repeated_error_is_throttled_but_the_bot_keeps_retrying_every_cycle",
         "test_a_different_error_type_is_not_blocked_by_a_previous_backoff", "test_a_bot_that_cannot_build_the_grid_for_lack_of_capital_does_not_spam_forever")],
     "critical": False, "impact": "Achado real do diagnóstico: um bot mal configurado podia alertar a cada minuto para sempre."},
    {"id": "al-watchdog", "cat": "Alertas", "name": "Vigia externo avisa se o corredor parar de dar sinal",
     "requirement": "Um serviço à parte (watchdog) avisa por Telegram/WhatsApp se o corredor deixar de responder.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_watchdog_warns_once_when_the_runner_stops_and_again_when_it_returns"),
                                T("test_config_alerts", "test_the_watchdog_uses_every_enabled_channel")],
     "critical": False, "impact": "Sem isto, um corredor morto só se notava ao abrir o painel."},

    # ---------------- 7. Segurança ----------------
    {"id": "seg-isolamento", "cat": "Segurança", "name": "Só trader.py fala com a Testnet; o motor nunca toca na rede",
     "requirement": "Nenhum outro módulo tem POST/DELETE nem o endereço da Testnet; engine.py não importa rede.",
     "how": "pytest", "tests": [T("test_testnet", "test_trader_only_talks_to_the_testnet_and_never_reads_key_files"),
                                T("test_testnet", "test_only_trader_can_send_or_cancel_orders_and_the_engine_never_touches_the_network"),
                                T("test_bots", "test_no_real_order_code_exists_in_the_bot_modules")],
     "critical": True, "impact": "É a garantia estrutural de que a conta real nunca é tocada."},
    {"id": "seg-chave-real", "cat": "Segurança", "name": "A chave real (só leitura) nunca é lida pelo código de ordens",
     "requirement": "O ficheiro da chave da conta real não é acedido por nenhum caminho que envie ordens.",
     "how": "pytest", "tests": [T("test_testnet", "test_real_key_file_is_never_used_by_the_order_code"),
                                T("test_portfolio", "test_no_order_sell_or_withdraw_code_exists"), T("test_portfolio", "test_permission_check_rules")],
     "critical": True, "impact": "Confunde a chave real com a da Testnet seria o pior cenário possível."},
    {"id": "seg-csp", "cat": "Segurança", "name": "Cabeçalhos de segurança (CSP com nonce) e /setup só local",
     "requirement": "Content-Security-Policy com nonce por pedido; a criação da palavra-passe só a partir deste computador.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_panel_headers_nonce_and_setup_only_from_this_computer"),
                                T("test_v03_fixes", "test_currency_switch_ignores_requests_from_other_sites")],
     "critical": True, "impact": "Sem isto, o painel ficava exposto a scripts injetados ou pedidos de outros sítios."},
    {"id": "seg-chaves-privadas", "cat": "Segurança", "name": "Ficheiros de chaves gravados com permissões restritas",
     "requirement": "As chaves (Testnet, Telegram, WhatsApp) ficam com permissões só para o utilizador; o registo de auditoria não cresce para sempre.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_keys_are_written_privately_and_the_audit_log_is_pruned"),
                                T("test_v03_fixes", "test_data_and_key_folders_come_from_the_environment")],
     "critical": True, "impact": "Chaves com permissões abertas seriam legíveis por outra conta do mesmo computador."},
    {"id": "seg-sem-segredo-ia", "cat": "Segurança", "name": "O resumo para a IA nunca leva chaves nem IDs de conta",
     "requirement": "Um verificador bloqueia o resumo se detetar uma chave conhecida ou uma sequência que pareça uma.",
     "how": "pytest", "tests": [T("test_portfolio2", "test_ai_summary_has_no_secrets_and_verifier_catches_leaks"),
                                T("test_portfolio2", "test_ai_summary_is_blocked_if_it_would_leak_a_secret")],
     "critical": True, "impact": "É o único conteúdo pensado para saír para um serviço externo (colado à mão)."},
    {"id": "seg-lock-unico", "cat": "Segurança", "name": "Só um corredor pode correr de cada vez",
     "requirement": "Um ficheiro de bloqueio impede dois corredores a enviar ordens em simultâneo.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_only_one_runner_can_hold_the_lock")],
     "critical": True, "impact": "Dois corredores ao mesmo tempo duplicariam ordens de forma imprevisível."},

    # ---------------- 8. Testes ----------------
    {"id": "te-suite-completa", "cat": "Testes", "name": "Suite completa a passar",
     "requirement": "Todos os testes automatizados do projeto passam nesta validação.",
     "how": "suite", "critical": True, "impact": "Uma regressão aqui pode afetar qualquer parte do sistema."},
    {"id": "te-sem-apagar", "cat": "Testes", "name": "Nenhum teste foi apagado para 'ficar verde'",
     "requirement": "Regra de trabalho seguida em todas as fases: uma falha investiga-se, nunca se apaga o teste.",
     "how": "manual", "status": NAO_VALIDADO,
     "evidence": "Prática seguida e registada nos relatórios de cada fase; esta validação automática não confirma "
                 "isso por código (teria de comparar o histórico de testes entre versões).",
     "critical": False, "impact": "Confiança no processo, não um requisito técnico isolado."},
    {"id": "te-mutacao", "cat": "Testes", "name": "Testes validados por mutação (reintroduzir bugs antigos)",
     "requirement": "Cada fase reintroduziu os bugs conhecidos numa cópia do código e confirmou que os testes falham.",
     "how": "manual", "status": NAO_VALIDADO,
     "evidence": "Foi feito manualmente, uma vez, em cada fase (Fase 1: 8 mutações, Fase 2: 5, capacidade: 9, "
                 "backoff/RAM/vigilante: 5). Esta validação não volta a correr essas mutações agora.",
     "critical": False, "impact": "Reforça a confiança nos testes, mas não é repetido automaticamente."},

    # ---------------- 9. Desempenho / Capacidade ----------------
    {"id": "de-monitor", "cat": "Desempenho / Capacidade", "name": "Indicadores reais (runner, erros, fila, CPU, RAM, disco, rede)",
     "requirement": "O estado é uma combinação destes sinais, nunca um valor isolado, com limiares documentados.",
     "how": "pytest", "tests": [T("test_capacity", n) for n in (
         "test_normal_with_many_bots_when_the_machine_has_margin", "test_low_cpu_but_late_runner_and_failing_requests_is_critical",
         "test_several_signals_together_push_the_bar_up_but_only_with_a_system_symptom")],
     "critical": False, "impact": "Só informa; não altera bots."},
    {"id": "de-ram-atribuicao", "cat": "Desempenho / Capacidade", "name": "RAM do sistema não é confundida com a da central",
     "requirement": "RAM alta de outros programas não gera ATENÇÃO se a app estiver confirmada saudável.",
     "how": "pytest", "tests": [T("test_capacity_fixes", n) for n in (
         "test_high_ram_from_other_programs_does_not_raise_the_state_when_the_app_itself_is_confirmed_healthy",
         "test_the_app_itself_using_a_lot_of_ram_is_a_real_signal_not_suppressed",
         "test_ram_state_is_unchanged_when_process_measurement_is_unavailable_fallback")],
     "critical": False, "impact": "Achado real do diagnóstico de 2026-09-27: RAM global do Windows gerava falso ATENÇÃO."},
    {"id": "de-sem-falso-alerta", "cat": "Desempenho / Capacidade", "name": "Sem falsos alertas (histerese, mediana)",
     "requirement": "Um pico isolado não muda o estado; subir exige 2 leituras seguidas, descer exige 5.",
     "how": "pytest", "tests": [T("test_capacity", n) for n in (
         "test_a_single_slow_cycle_or_cpu_spike_creates_no_alert", "test_two_consecutive_slow_cycles_are_a_real_signal",
         "test_recovery_needs_several_good_cycles_and_is_not_flapping")],
     "critical": False, "impact": "Sem isto, o painel oscilaria de estado a cada ciclo ruidoso."},
    {"id": "de-vigilante-leve", "cat": "Desempenho / Capacidade", "name": "Vigilante do PARAR TUDO não gasta rede sem necessidade",
     "requirement": "Com a flag desligada, não lê o ficheiro de chaves nem constrói ligação à Testnet.",
     "how": "pytest", "tests": [T("test_capacity_fixes", "test_watcher_never_builds_a_trader_or_reads_keys_when_the_flag_is_off"),
                                T("test_capacity_fixes", "test_watcher_builds_the_trader_and_sweeps_once_the_flag_turns_on")],
     "critical": False, "impact": "Poupa uma leitura de disco a cada 5 s, 24 h por dia."},
    {"id": "de-diagnostico-real", "cat": "Desempenho / Capacidade", "name": "Diagnóstico de capacidade com dados reais (2 bots, 265 min)",
     "requirement": "0 erros, 0 timeouts, ciclo médio 1,51 s de 60 s, ~2 pedidos/min por bot, sem crescimento de RAM da app.",
     "how": "manual", "status": PASSOU,
     "evidence": "Diagnóstico de 2026-09-27: 53 janelas de 5 min com os bots 19/20 reais na Testnet; RAM do processo "
                 "do corredor estável entre 44,8 e 45,6 MB ao longo de mais de 4 h.",
     "critical": False, "impact": "Confirma que o consumo atual não é excessivo, nesta máquina."},
    {"id": "de-sem-leak-continuo", "cat": "Desempenho / Capacidade", "name": "Ausência de fuga de memória confirmada continuamente",
     "requirement": "Monitorização automática e recorrente (não só um diagnóstico manual pontual) que confirme não haver fuga.",
     "how": "manual", "status": NAO_VALIDADO,
     "evidence": "O diagnóstico de 2026-09-27 foi manual e pontual (4 amostras ao longo de ~2 h). Não existe ainda "
                 "uma verificação automática e contínua desta propriedade.",
     "critical": False, "impact": "Uma fuga lenta (ao longo de semanas) não seria apanhada por um diagnóstico pontual."},

    # ---------------- 10. Raspberry Pi ----------------
    {"id": "pi-wal", "cat": "Raspberry Pi", "name": "SQLite em WAL com espera longa por bloqueios",
     "requirement": "Menos escritas no cartão SD, sem bloquear entre o painel e o corredor.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_database_uses_wal_and_long_lock_waits")],
     "critical": False, "impact": "Sem isto, um cartão SD desgastava-se mais depressa."},
    {"id": "pi-pastas-env", "cat": "Raspberry Pi", "name": "Pastas de dados e chaves configuráveis por variável de ambiente",
     "requirement": "BOTS_DATA_DIR e BOTS_KEYS_DIR permitem instalar fora da pasta do utilizador do Claude Code.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_data_and_key_folders_come_from_the_environment")],
     "critical": False, "impact": "Sem isto, a instalação num Pi ficaria presa a um caminho fixo."},
    {"id": "pi-relogio", "cat": "Raspberry Pi", "name": "Código corrige o desvio do relógio (Pi sem RTC)",
     "requirement": "Um Pi sem relógio com pilha arranca com a hora errada; o corredor compensa a diferença.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_trader_corrects_its_clock_instead_of_pausing_bots")],
     "critical": False, "impact": "Sem isto, ordens assinadas com a hora errada seriam sempre recusadas ao arrancar."},
    {"id": "pi-backup", "cat": "Raspberry Pi", "name": "Cópia de segurança automática e consistente",
     "requirement": "Existe um mecanismo de backup que produz uma cópia íntegra e limpa as antigas.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_backup_is_consistent_and_old_copies_are_removed")],
     "critical": False, "impact": "Código testado; falta confirmar a execução real e agendada num Pi."},
    {"id": "pi-deploy-ficheiros", "cat": "Raspberry Pi", "name": "Ficheiros de instalação e serviço existem no repositório",
     "requirement": "systemd (painel, corredor, vigilante), script de instalação, apontam para os comandos certos.",
     "how": "pytest", "tests": [T("test_v03_fixes", "test_deploy_files_exist_and_point_at_the_right_commands")],
     "critical": False, "impact": "Confirma que o código de instalação existe; não confirma que correu num Pi."},
    {"id": "pi-real", "cat": "Raspberry Pi", "name": "Corrido de facto num Raspberry Pi real",
     "requirement": "O projeto instalado e a correr 24/7 num Raspberry Pi verdadeiro, não só simulado nesta máquina.",
     "how": "manual", "status": NAO_VALIDADO,
     "evidence": "Nunca foi instalado nem corrido num Raspberry Pi físico. Todos os testes correm contra uma "
                 "exchange falsa, nesta máquina Windows.", "critical": True,
     "impact": "É o destino final planeado do projeto; sem este teste há sempre incerteza sobre hardware real."},
    {"id": "pi-testnet-real", "cat": "Raspberry Pi", "name": "Ordens enviadas de facto à Binance Testnet real",
     "requirement": "Pelo menos um envio/cancelamento real contra a Testnet verdadeira da Binance, não só a exchange falsa dos testes.",
     "how": "manual", "status": NAO_VALIDADO,
     "evidence": "Os 346 testes usam sempre uma exchange falsa em memória. O envio real de ordens à Testnet "
                 "verdadeira da Binance nunca foi confirmado nesta sessão.", "critical": True,
     "impact": "Há sempre a possibilidade de a API real se comportar de forma diferente da exchange falsa."},

    # ---------------- 11. Operação 24/7 ----------------
    {"id": "op-7dias-sintetico", "cat": "Operação 24/7", "name": "7 dias sintéticos sem intervenção (simulação e Testnet falsa)",
     "requirement": "Uma sequência de mercado de 7 dias, processada ao ritmo real do corredor, sem erros nem intervenção.",
     "how": "pytest", "tests": [T("test_bots", "test_seven_days_without_intervention_meets_the_v02_criteria"),
                                T("test_testnet", "test_seven_days_on_the_testnet_without_intervention")],
     "critical": False, "impact": "É uma simulação acelerada, não 7 dias reais de relógio."},
    {"id": "op-heartbeat", "cat": "Operação 24/7", "name": "Sinal de vida do corredor e aviso se parar",
     "requirement": "O painel mostra quando o corredor deu sinal por último; o vigia externo avisa se parar.",
     "how": "pytest", "tests": [T("test_bots", "test_bots_page_warns_when_the_runner_is_not_running"),
                                T("test_v03_fixes", "test_watchdog_warns_once_when_the_runner_stops_and_again_when_it_returns")],
     "critical": False, "impact": "Sem isto, um corredor parado só se notava manualmente."},
    {"id": "op-24-7-real", "cat": "Operação 24/7", "name": "Corrido de facto 24 horas por dia, vários dias, sem supervisão",
     "requirement": "Operação real e contínua, não uma sequência sintética acelerada em segundos de teste.",
     "how": "manual", "status": NAO_VALIDADO,
     "evidence": "O corredor já correu várias horas seguidas nesta sessão (incluindo o diagnóstico de capacidade "
                 "de 265 min), mas nunca dias seguidos sem supervisão, e foi parado pelo menos uma vez por falta "
                 "de memória do próprio Windows (não do projeto).", "critical": False,
     "impact": "É o teste final de robustez antes de confiar o sistema a correr sozinho."},
    {"id": "op-reinicio-automatico", "cat": "Operação 24/7", "name": "Reinício automático depois de um crash real (systemd)",
     "requirement": "Se o processo morrer, o sistema operativo reinicia-o sozinho (Restart= no systemd).",
     "how": "manual", "status": NAO_VALIDADO,
     "evidence": "O ficheiro de serviço existe e tem essa configuração (verificado por `pi-deploy-ficheiros`), mas "
                 "nunca foi exercitado com um crash real sob systemd. Nesta sessão, um crash exigiu reinício manual.",
     "critical": True, "impact": "Sem isto confirmado, um crash real num Pi sem vigilância podia deixar o sistema parado."},
]

_INDEX = {item["id"]: item for item in CHECKLIST}
CATEGORIES = list(dict.fromkeys(item["cat"] for item in CHECKLIST))


# ---------- execução ----------
_LINE = re.compile(r"^(?P<nodeid>\S+::\S+)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED)\b")

# ---------- progresso ao vivo (para a janela flutuante do painel; um processo, em memória) ----------
_progress_lock = threading.Lock()
_progress = {"running": False, "run_id": 0, "mode": None, "total": 0, "current": None, "done": []}


def _progress_reset(total, mode):
    with _progress_lock:
        _progress.update(running=True, run_id=_progress["run_id"] + 1, mode=mode, total=total,
                          current=None, done=[])
        return _progress["run_id"]


def _progress_current(run_id, item):
    with _progress_lock:
        if _progress["run_id"] == run_id:
            _progress["current"] = {"id": item["id"], "name": item["name"], "cat": item["cat"]}


def _progress_done(run_id, item, status):
    with _progress_lock:
        if _progress["run_id"] == run_id:
            _progress["done"].append({"id": item["id"], "name": item["name"], "cat": item["cat"], "status": status})
            _progress["current"] = None


def _progress_finish(run_id):
    with _progress_lock:
        if _progress["run_id"] == run_id:
            _progress["running"] = False


def progress_snapshot():
    """Cópia do estado atual (nunca a instância partilhada): para o painel sondar sem arriscar corrida de dados."""
    with _progress_lock:
        return {**_progress, "done": list(_progress["done"])}


def is_running():
    with _progress_lock:
        return _progress["running"]


def begin():
    """Marca "a correr" já no pedido HTTP, antes do fio em segundo plano arrancar de facto — sem isto, quem
    sondar `is_running()` logo a seguir podia apanhar a janela entre o pedido e o fio começar e achar que já
    tinha terminado."""
    with _progress_lock:
        _progress["running"] = True


def mark_idle():
    """Rede de segurança: se `run()` rebentar antes de chegar ao fim, isto garante que a janela flutuante não
    fica presa em "a correr" para sempre."""
    with _progress_lock:
        _progress["running"] = False


def _run_suite(node_ids=None, on_result=None):
    """Corre a suite (toda, ou só os node ids indicados) UMA vez, em modo verboso, e devolve
    (resumo_texto, {node_id: outcome}, duração_s).

    Lê a saída do pytest linha a linha, à medida que cada teste termina — não só no fim — e chama
    `on_result(node_id, outcome)` logo que reconhece cada resultado. Sem isto, um item que dependa de vários
    testes só saberia o seu estado depois de TODA a suite acabar, mesmo que os seus testes específicos já tivessem
    terminado há muito: a janela flutuante ficaria parada em "0 de N" até ao fim, em vez de ir contando a sério.
    """
    t0 = time.time()
    targets = list(node_ids) if node_ids else [str(ROOT / "tests")]
    outcomes = {}
    tail_lines = []
    try:
        proc = subprocess.Popen(
            [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "pytest", *targets, "-p", "no:cacheprovider",
             "--color=no", "-v", "--tb=short"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            if time.time() - t0 > 900:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd="pytest", timeout=900)
            line = line.rstrip("\n")
            tail_lines.append(line)
            if len(tail_lines) > 6:
                tail_lines.pop(0)
            m = _LINE.match(line.strip())
            if m:
                nodeid = m.group("nodeid").replace("\\", "/")
                if not nodeid.startswith("tests/"):
                    nodeid = "tests/" + nodeid
                outcomes[nodeid] = m.group("outcome")
                if on_result:
                    on_result(nodeid, m.group("outcome"))
        proc.stdout.close()
        proc.wait(timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Não consegui correr os testes: {exc}", outcomes, time.time() - t0
    return "\n".join(tail_lines), outcomes, time.time() - t0


def _pytest_item_result(item, outcomes):
    found = {t: outcomes.get(t) for t in item["tests"]}
    missing = [t for t, o in found.items() if o is None]
    if missing:
        return NAO_VALIDADO, found, f"Não encontrei resultado para: {', '.join(missing)} (a suite pode não ter corrido até ao fim)."
    if all(o == "PASSED" for o in found.values()):
        return PASSOU, found, None
    if any(o == "FAILED" or o == "ERROR" for o in found.values()):
        failed = [t for t, o in found.items() if o in ("FAILED", "ERROR")]
        return FALHOU, found, f"Falhou: {', '.join(failed)}"
    return PARCIAL, found, f"Resultado misto: {found}"


def _file_item_result(item):
    def resolve(p):
        p = Path(p)
        return p if p.is_absolute() else ROOT / p

    missing = [str(p) for p in item["paths"] if not resolve(p).exists()]
    if missing:
        return FALHOU, {str(p): resolve(p).exists() for p in item["paths"]}, f"Em falta: {', '.join(missing)}"
    return PASSOU, {str(p): True for p in item["paths"]}, None


def run(persist_conn=None, mode="tudo"):
    """Corre a checklist. `mode="tudo"` reverifica tudo; `mode="pendentes"` só reverifica o que a última validação
    guardada não tinha PASSOU (e qualquer item novo) — os restantes mantêm exatamente o resultado anterior, nunca
    um valor inventado. Sem validação anterior guardada, "pendentes" também corre tudo (não há nada para reaproveitar).
    Devolve o registo completo (para gravar e para o painel); reporta progresso item a item em `progress_snapshot()`.
    """
    t0 = time.time()
    last = load_last(persist_conn) if persist_conn is not None else None
    carried = {r["id"]: r for r in (last["results"] if last else [])}
    if mode == "pendentes" and last:
        reverify_ids = {item["id"] for item in CHECKLIST if carried.get(item["id"], {}).get("status") != PASSOU}
    else:
        reverify_ids = {item["id"] for item in CHECKLIST}

    run_id = _progress_reset(len(reverify_ids), mode)

    need_full_suite = "te-suite-completa" in reverify_ids
    pytest_items = [item for item in CHECKLIST if item["id"] in reverify_ids and item["how"] == "pytest"]
    node_ids = sorted({t for item in pytest_items for t in item["tests"]})

    # progresso ao vivo dos itens "pytest": reporta-os assim que os SEUS testes terminam, não só no fim da suite
    # inteira — senão a janela flutuante ficava parada em "0 de N" durante os 1-2 minutos da suite completa.
    reported_live = set()
    live_outcomes = {}

    def on_result(nodeid, outcome):
        live_outcomes[nodeid] = outcome
        for pi in pytest_items:
            if pi["id"] in reported_live or nodeid not in pi["tests"]:
                continue
            if all(t in live_outcomes for t in pi["tests"]):
                status, _, _ = _pytest_item_result(pi, live_outcomes)
                _progress_done(run_id, pi, status)
                reported_live.add(pi["id"])

    if need_full_suite:
        tail, outcomes, suite_dur = _run_suite(on_result=on_result)
        n_total = len(outcomes)
        n_failed = sum(1 for o in outcomes.values() if o in ("FAILED", "ERROR"))
        n_passed = sum(1 for o in outcomes.values() if o == "PASSED")
    elif node_ids:
        tail, outcomes, suite_dur = _run_suite(node_ids, on_result=on_result)
        n_total = last["suite_total"] if last else 0     # cabeçalho: mantém a última contagem real da suite completa
        n_passed = last["suite_passed"] if last else 0
        n_failed = last["suite_failed"] if last else 0
    else:
        tail, outcomes, suite_dur = (last["suite_tail"] if last else ""), {}, 0.0
        n_total = last["suite_total"] if last else 0
        n_passed = last["suite_passed"] if last else 0
        n_failed = last["suite_failed"] if last else 0

    results = []
    for item in CHECKLIST:
        if item["id"] not in reverify_ids:
            results.append(carried[item["id"]])
            continue
        already_live = item["id"] in reported_live
        if not already_live:
            _progress_current(run_id, item)
        it0 = time.time()
        error = None
        if item["how"] == "pytest":
            status, obtained, error = _pytest_item_result(item, outcomes)
            obtained_text = "; ".join(f"{t.split('::')[-1]}: {o}" for t, o in obtained.items())
            evidence = ", ".join(item["tests"])
        elif item["how"] == "file":
            status, obtained, error = _file_item_result(item)
            obtained_text = json.dumps(obtained, ensure_ascii=False)
            evidence = ", ".join(item["paths"])
        elif item["how"] == "suite":
            if n_total == 0:
                status, obtained_text = NAO_VALIDADO, "A suite não produziu resultados (ver 'tail' do processo)."
                error = tail
            elif n_failed == 0:
                status, obtained_text = PASSOU, f"{n_passed} passed / {n_failed} failed (de {n_total})"
            else:
                status, obtained_text = FALHOU, f"{n_passed} passed / {n_failed} failed (de {n_total})"
                error = tail
            evidence = "suite completa (tests/)"
        else:  # manual
            status = item["status"]
            obtained_text = item["evidence"]
            evidence = item["evidence"]
        results.append({
            "id": item["id"], "cat": item["cat"], "name": item["name"], "requirement": item["requirement"],
            "how": item["how"], "status": status, "obtained": obtained_text, "evidence": evidence,
            "error": error, "critical": item["critical"], "impact": item["impact"],
            "duration_s": round(time.time() - it0, 3),
        })
        if not already_live:
            _progress_done(run_id, item, status)

    counts = {s: sum(1 for r in results if r["status"] == s) for s in STATES}
    blockers = [r for r in results if r["critical"] and r["status"] in (FALHOU, PARCIAL, NAO_VALIDADO)]
    if any(r["status"] == FALHOU for r in blockers) or any(r["status"] == PARCIAL for r in blockers):
        verdict = "nao_pronto"
    elif blockers:
        verdict = "nao_validado"
    else:
        verdict = "pronto"

    run_record = {
        "ts": time.time(), "duration_s": round(time.time() - t0, 1), "suite_duration_s": round(suite_dur, 1),
        "suite_total": n_total, "suite_passed": n_passed, "suite_failed": n_failed, "suite_tail": tail,
        "total_items": len(results), "counts": counts, "verdict": verdict,
        "blockers": [r["id"] for r in blockers], "results": results,
        "commit": _git_commit(),
    }
    if persist_conn is not None:
        save(persist_conn, run_record)
    _progress_finish(run_id)
    return run_record


def _git_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), capture_output=True, text=True, timeout=10)
        head = r.stdout.strip() if r.returncode == 0 else "sem git"
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True, timeout=10)
        return head + (" +alterações não gravadas" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "desconhecido"


# ---------- persistência (settings, como capacity_snapshot) ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS validation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, verdict TEXT NOT NULL, total_items INTEGER NOT NULL,
    passou INTEGER NOT NULL, falhou INTEGER NOT NULL, parcial INTEGER NOT NULL, nao_validado INTEGER NOT NULL,
    na INTEGER NOT NULL, blockers INTEGER NOT NULL, suite_total INTEGER NOT NULL, suite_passed INTEGER NOT NULL,
    suite_failed INTEGER NOT NULL, commit_ TEXT NOT NULL
);
"""
HISTORY_KEEP = 30


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def save(conn, run_record):
    init(conn)
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('validation_last', ?)",
                     (json.dumps(run_record, ensure_ascii=False),))
        c = run_record["counts"]
        conn.execute("INSERT INTO validation_history (ts, verdict, total_items, passou, falhou, parcial, "
                     "nao_validado, na, blockers, suite_total, suite_passed, suite_failed, commit_) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (int(run_record["ts"]), run_record["verdict"], run_record["total_items"], c[PASSOU], c[FALHOU],
                      c[PARCIAL], c[NAO_VALIDADO], c[NA], len(run_record["blockers"]), run_record["suite_total"],
                      run_record["suite_passed"], run_record["suite_failed"], run_record["commit"]))
        conn.execute("DELETE FROM validation_history WHERE id NOT IN "
                     "(SELECT id FROM validation_history ORDER BY id DESC LIMIT ?)", (HISTORY_KEEP,))


def load_last(conn):
    try:
        raw = db.get(conn, "validation_last")
        return json.loads(raw) if raw else None
    except (ValueError, TypeError) as exc:
        log.warning("validation_last inválido: %s", exc)
        return None


def history(conn, limit=HISTORY_KEEP):
    init(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM validation_history ORDER BY id DESC LIMIT ?", (limit,))]


# ---------- vista para o painel ----------
VERDICT_LABEL = {"pronto": "🟢 PRONTO PARA AUDITORIA FINAL", "nao_pronto": "🔴 NÃO PRONTO — EXISTEM BLOQUEADORES",
                "nao_validado": "🟡 NÃO VALIDADO — FALTAM EVIDÊNCIAS"}
VERDICT_PILL = {"pronto": "gain", "nao_pronto": "loss", "nao_validado": "warn"}


def view(conn):
    run_record = load_last(conn)
    if not run_record:
        return {"available": False}
    by_cat = {}
    for r in run_record["results"]:
        by_cat.setdefault(r["cat"], []).append(r)
    categories = []
    for cat in CATEGORIES:
        items = by_cat.get(cat, [])
        ok = sum(1 for r in items if r["status"] == PASSOU or r["status"] == NA)
        dot = "🟢" if all(r["status"] in (PASSOU, NA) for r in items) else \
            ("🔴" if any(r["status"] == FALHOU for r in items) else "🟡")
        categories.append({"cat": cat, "checks": items, "ok": ok, "total": len(items), "dot": dot,
                           "pct": round(ok / len(items) * 100) if items else 0})
    blockers = [r for r in run_record["results"] if r["id"] in run_record["blockers"]]
    return {"available": True, "run": run_record, "categories": categories, "blockers": blockers,
            "verdict_label": VERDICT_LABEL[run_record["verdict"]], "verdict_pill": VERDICT_PILL[run_record["verdict"]],
            "history": history(conn), "icons": ICONS, "labels": LABELS, "pills": PILL}
