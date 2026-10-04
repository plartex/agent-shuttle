# Qriterra и Antigravity: проверка 80 правил Agent Shuttle

Дата: 3 октября 2026 года. Объект: исходный снимок репозитория Agent Shuttle. Qriterra находится в отдельном репозитории `agent-code-checker`.

## Результат

Qriterra получила ответы по **всем 80 правилам** Code Smells: **47 прошли, 33 обнаружили запах, 0 ошибок протокола, 0 неопределённых**. Все 33 находки содержат цитаты, прошедшие строгую сверку с файлами исходного снимка. Машиночитаемый отчёт с полными аргументами и всеми цитатами: [report.verified.json](../../.runtime/qriterra-verified-2026-10-03/report.verified.json).

В снимок вошли 64 файла, и содержимое всех 64 было передано модели. Модель **заявила просмотр только 42 из них** (65.6%). Поэтому `quality_score = null`: полноту анализа каждого файла подтвердить нельзя, даже при 100% ответов по правилам. Статус `passed` означает «по данному правилу модель не сообщила находку в заявленном охвате», а не математическое доказательство отсутствия запаха во всём проекте. Находки с валидными цитатами требуют инженерной оценки приоритета.

Причина ограничения охвата видна в сохранённых ответах: все 80 результатов повторяют один и тот же список из 42 `inspected_paths` и помечают его как `complete`. Запрос просил изучить каждый *релевантный* файл, оставив модели выбор. Валидатор требовал непустой список существующих файлов, но не сравнивал `complete` с полным манифестом из 64 файлов; агрегатор обнаружил пропущенные 22 файла только при построении отчёта. Эти данные **не доказывают**, что модель физически не читала остальные файлы: они доказывают отсутствие подтверждённого охвата. Среди 22 незаявленных файлов — конфигурация, примеры и часть тестов.

## Как проведён запуск

- Один native ID Antigravity: `b77db479-3500-4650-a1d2-56b12dc5d0a4`. Модель `gemini-3.8-flash-high`, reasoning effort `high`.
- Нативный `read_only` проверен отрицательной пробой на запись до задач. После отклонённого чтения native transcript задача продолжилась **в той же сессии**.
- Запросы передавали пакет правил, точную схему JSON и содержимое снимка. Qriterra проверяла каждое правило независимо, сохраняла валидные соседние ответы, исправляла неверные ответы только по нужным ID и фиксировала прогресс.
- Первоначальная финальная проверка ошибочно сравнила снимок, найденный через файловую систему, со списком из Git. Игнорируемый Git файл `bin/manifest.json` в результате выглядел как исчезнувший; все ответы были ошибочно помечены `inconclusive`. Мы воспроизвели это регрессионным тестом и закрепили режим инвентаризации снимка на весь прогон. Сохранённые ответы повторно проверены **без модельных вызовов**: SHA-256 всех исходных файлов совпали, схема и цитаты валидны, итог 47/33/0.
- После снимка внесены точечные изменения в `src/agent_shuttle/a2a_server.py`, `src/agent_shuttle/backends.py`, `src/agent_shuttle/client.py` и добавлен `tests/test_failed_usage.py`. Строгая повторная проверка **всех 80 результатов и цитат 33 находок** по текущей рабочей копии прошла, но семантические оценки относятся к исходному снимку; новый тест обзором модели не охвачен.

## Исправления по TDD

1. **Запуск Antigravity и права.** Предпроверка работоспособности, повтор при известных стартовых сбоях с ограничением времени, проверка реально применённой нативной политики. Запрошенные модель и политика сохраняются.
2. **Структурированный ответ.** Строгая JSON Schema, нативная команда завершения и сверка свежести результата по текущему ходу. Неверный или старый результат даёт явную ошибку; сессия остаётся пригодной после исправимого отказа.
3. **Qriterra.** Контракт ответа 2.0, валидация цитат и покрытия, восстановление валидных правил пакета, ограничение числа исправлений и времени, учёт неизменности исходников. Незаявленные файлы предотвращают публикацию общего балла.
4. **Учёт токенов.** Расход неуспешного хода передаётся через A2A и виден клиенту. Предпроверка учитывается один раз. Отдельные регрессионные тесты сначала воспроизводили потерю этих данных и двойной счёт.

Проверка Qriterra: **89 тестов прошли**. В Agent Shuttle до последних двух регрессионных тестов полный прогон дал **276 прошедших, 8 пропущенных**. В последнем ограниченном запуске 278 тестов завершились с двумя таймаутами тестов отмены. Диагностика текущего окружения показала, что даже `socket.socketpair()` и создание Windows asyncio loop зависают; поэтому последний полный сетевой прогон не считаем зелёным. Разрешение на запуск с локальным loopback дважды не было обработано автоматической проверкой до срока. Код теста не меняли для обхода ограничения. Журналы: `.runtime/tests-reliability-final.log` и `.runtime/qriterra-tests-final.log`.

## Расход токенов

Для единственной native-сессии полного обзора: **1,486,409 входных + 213,586 выходных = 1,699,995 total**. Показатель cache read — **23,719,906**, он дан отдельно и к total не прибавляется. В total восстановлен отказанный ход: 216,046 токенов, ранее не дошедших через A2A.

Отдельные успешные диагностические вызовы этого этапа добавили **297 457** токенов; измеренный минимум этапа — **1 997 452**. У нескольких прерванных пилотных вызовов счётчик не вернулся, поэтому это нижняя граница, а не полный биллинг. Более ранний отдельный запуск 2 октября отражён в [предыдущем отчёте](antigravity-qriterra-report-2026-10-02.md).

## Обнаруженные запахи (33)

| Правило | Краткий вывод модели | Первая цитата |
| --- | --- | --- |
| `long_method` | make_app in a2a_server.py is an excessively long method (187 lines) mixing configuration, route handlers, and lifespan supervision. | `src/agent_shuttle/a2a_server.py:352` |
| `long_parameter_list` | SessionManager.run accepts 8 parameters in its signature. | `src/agent_shuttle/a2a_server.py:59` |
| `switch_statements` | CLI server startup uses if/elif branches based on agent type strings instead of polymorphic instantiation. | `src/agent_shuttle/cli.py:97` |
| `type_introspection_downcasting` | isinstance type checks and concrete attribute downcasting in identity_data inspect backend classes directly. | `src/agent_shuttle/a2a_server.py:400` |
| `duplicate_code` | Identical environment variable whitelist filtering block duplicated across opencode_runtime.py and claude_runtime.py. | `src/agent_shuttle/opencode_runtime.py:95` |
| `dead_code` | SessionManager class in a2a_server.py is unused in production code. | `src/agent_shuttle/a2a_server.py:49` |
| `redundant_wrapper` | Task in task_library.py and TaskHandle in client.py define multiple one-line pass-through methods that forward arguments directly to underlying service objects without adding value. | `src/agent_shuttle/task_library.py:81` |
| `feature_envy` | ProfiledInfo.fetch extensively navigates and extracts data from AgentProfile rather than having the profile format its own capabilities. | `src/agent_shuttle/profiled.py:74` |
| `message_chains` | Long property access chains traversing deeply into nested A2A message structures in task_events. | `src/agent_shuttle/client.py:292` |
| `hidden_side_effects` | verify_probe in ScopedAgyPolicy performs hidden state mutation on disk by writing and replacing policy configuration files. | `src/agent_shuttle/agy_policy.py:127` |
| `deeply_nested_code` | Control flow in _ask_within_deadline reaches 6 levels of nesting inside the readline loop. | `src/agent_shuttle/backends.py:721` |
| `flag_argument` | policy_probe boolean parameter in _ask_within_deadline switches internal execution and validation paths. | `src/agent_shuttle/backends.py:664` |
| `magic_numbers_strings` | Hardcoded magic numbers for character budgets and chunk sizes in task_transcript. | `src/agent_shuttle/client.py:252` |
| `swallow_exception` | Empty catch block in ProfiledBackend.run silences exceptions during session close. | `src/agent_shuttle/profiled.py:48` |
| `catch_generic_exception` | Broad catch of top-level Exception in BridgeExecutor._execute_library. | `src/agent_shuttle/a2a_server.py:258` |
| `exception_as_flow_control` | Session closing route handler uses KeyError exception handling as normal control flow to set response status rather than checking session existence. | `src/agent_shuttle/a2a_server.py:481` |
| `large_class` | TaskManager is a God Class absorbing database persistence, process execution, session caching, and event streaming. | `src/agent_shuttle/task_library.py:134` |
| `primitive_obsession` | Primitive Obsession in _SessionRecord which represents configuration options as an untyped primitive tuple. | `src/agent_shuttle/a2a_server.py:40` |
| `data_clumps` | Data clumps of model, reasoning_effort, read_only, and tool_policy travel together across method signatures without encapsulation. | `src/agent_shuttle/client.py:305` |
| `divergent_change` | Divergent change in TaskManager due to combining database schema, session lifecycle, and worker orchestration in one class. | `src/agent_shuttle/task_library.py:134` |
| `lazy_class` | Session is a lazy class doing virtually no independent work beyond forwarding calls to TaskManager. | `src/agent_shuttle/task_library.py:116` |
| `data_class` | TaskStatus is a passive data class containing only data fields without behavior. | `src/agent_shuttle/task_library.py:31` |
| `inappropriate_intimacy` | Inappropriate intimacy between SessionManager and _SessionRecord through direct manipulation of internal record state. | `src/agent_shuttle/a2a_server.py:84` |
| `middle_man` | Task is a Middle Man delegating 87.5% of its methods directly to TaskManager. | `src/agent_shuttle/task_library.py:76` |
| `insider_trading` | Modules directly import and consume private implementation members and helpers from neighboring modules. | `src/agent_shuttle/mcp_tasks.py:13` |
| `shotgun_surgery` | Shotgun surgery caused by scattered ad-hoc tool policy branching across backends and runtimes without polymorphic encapsulation. | `src/agent_shuttle/backends.py:234` |
| `leaky_abstraction` | Antigravity provider-specific parameters leak into generic harness and task manager interfaces. | `src/agent_shuttle/managed.py:77` |
| `layer_violation` | TaskManager bypasses persistence abstractions by directly invoking raw SQLite DDL scripts and OS file locking. | `src/agent_shuttle/task_library.py:198` |
| `hardcoded_environment_config` | Hardcoded loopback IP addresses and default service URLs across runtime configurations. | `src/agent_shuttle/managed.py:74` |
| `assertion_roulette` | Multiple assertions executed sequentially without individual diagnostic error messages. | `tests/test_library_tasks.py:71` |
| `eager_test` | Test method verifies multiple unrelated operations in a single test case. | `tests/test_library_tasks.py:65` |
| `conditional_test_logic` | Loop control structure wrapping test assertions. | `tests/test_task_store.py:219` |
| `in_memory_data_filtration` | SQLiteTaskStore loads the full set of owner task blobs into memory and filters, sorts, and paginates in Python rather than utilizing database query clauses and existing indexes. | `src/agent_shuttle/task_store.py:208` |

## Статус всех правил

| Правило | Статус |
| --- | --- |
| `long_method` | failed |
| `long_parameter_list` | failed |
| `long_chain_of_calls` | passed |
| `switch_statements` | failed |
| `type_introspection_downcasting` | failed |
| `duplicate_code` | failed |
| `dead_code` | failed |
| `comments_as_deodorant` | passed |
| `redundant_wrapper` | failed |
| `feature_envy` | failed |
| `message_chains` | failed |
| `hidden_side_effects` | failed |
| `mutating_input_arguments` | passed |
| `deeply_nested_code` | failed |
| `flag_argument` | failed |
| `output_parameters` | passed |
| `premature_optimization` | passed |
| `magic_numbers_strings` | failed |
| `swallow_exception` | failed |
| `catch_generic_exception` | failed |
| `throw_generic_exception` | passed |
| `destructive_rethrow` | passed |
| `log_and_throw` | passed |
| `exception_as_flow_control` | failed |
| `return_null_on_error` | passed |
| `resource_leak` | passed |
| `sensitive_data_exposure_logging` | passed |
| `disabled_certificate_validation` | passed |
| `large_class` | failed |
| `primitive_obsession` | failed |
| `data_clumps` | failed |
| `combinatorial_explosion` | passed |
| `refused_bequest` | passed |
| `alternative_classes_different_interfaces` | passed |
| `temporary_field` | passed |
| `anemic_domain_model` | passed |
| `freeloader_interface` | passed |
| `divergent_change` | failed |
| `parallel_inheritance_hierarchies` | passed |
| `lazy_class` | failed |
| `data_class` | failed |
| `inappropriate_intimacy` | failed |
| `middle_man` | failed |
| `insider_trading` | failed |
| `incomplete_library_class` | passed |
| `temporal_coupling` | passed |
| `non_thread_safe_singleton` | passed |
| `interface_bloat` | passed |
| `test_code_in_production` | passed |
| `shotgun_surgery` | failed |
| `speculative_generality` | passed |
| `god_object_brain_class` | passed |
| `spaghetti_code` | passed |
| `leaky_abstraction` | failed |
| `layer_violation` | failed |
| `hardcoded_environment_config` | failed |
| `busy_waiting` | passed |
| `unprotected_shared_state` | passed |
| `broken_double_checked_locking` | passed |
| `blocking_async_code` | passed |
| `unhandled_fire_and_forget` | passed |
| `cyclic_dependency` | passed |
| `assertion_roulette` | failed |
| `mystery_guest` | passed |
| `hardcoded_test_data` | passed |
| `eager_test` | failed |
| `sleepy_flaky_test` | passed |
| `conditional_test_logic` | failed |
| `ignored_disabled_test_rot` | passed |
| `n_plus_one_queries` | passed |
| `in_memory_data_filtration` | failed |
| `god_table` | passed |
| `sql_injection_concatenation` | passed |
| `unindexed_foreign_keys` | passed |
| `hardcoded_credentials` | passed |
| `prop_drilling` | passed |
| `direct_dom_manipulation` | passed |
| `giant_god_component` | passed |
| `zombie_subscriptions_leak` | passed |
| `duplicated_derived_state` | passed |

## Воспроизведение и артефакты

Полный JSON: `C:\Users\kkaas\OneDrive\Документы\ChatGPT\Пиринговая сеть агентов\agent-bridge\.runtime\qriterra-verified-2026-10-03\report.verified.json`. Сохранённые запросы, ответы, native ID и диагностические данные находятся в `C:\Users\kkaas\OneDrive\Документы\ChatGPT\Пиринговая сеть агентов\agent-bridge\.runtime\qriterra-verified-2026-10-03`. Скрипт `.runtime/revalidate_qriterra.py` восстанавливает отчёт из сохранённых ответов без новых модельных вызовов; `.runtime/verify_current_evidence.py` проверяет цитаты на текущих файлах.
