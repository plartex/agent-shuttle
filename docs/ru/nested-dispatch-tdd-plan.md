# Защита от вложенной диспетчеризации: план TDD

Статус: план переноса, реализован 2 октября 2026 года. Область: экземпляр Agent Shuttle, унаследованный процессом worker, не должен отправлять новые задачи, отменять задачи, завершать сессии или менять настройки маршрутизации. Верхнеуровневый экземпляр продолжает работать как прежде.

## Что делает оригинал

Первоисточник — `FeiZhuLulu/Agent-Bridge`:

1. При запуске настоящего worker функция [`build_worker_env`](https://github.com/FeiZhuLulu/Agent-Bridge/blob/main/src/agent_bridge/worker_env.py) **после слияния** настроек принудительно ставит `AGENT_BRIDGE_PARENT_CONTEXT=worker` и направляет `AGENT_BRIDGE_HOME` в `nested/`. ACP и Antigravity адаптеры вызывают ее с `worker_context=True` ([ACP](https://github.com/FeiZhuLulu/Agent-Bridge/blob/main/src/agent_bridge/adapters/acp.py), [Antigravity](https://github.com/FeiZhuLulu/Agent-Bridge/blob/main/src/agent_bridge/adapters/antigravity.py)).
2. [`paths.py`](https://github.com/FeiZhuLulu/Agent-Bridge/blob/main/src/agent_bridge/paths.py) распознает только точное значение `worker`; даже если host переопределил home, маркер добавляет `nested/` при чтении пути.
3. [`Registry`](https://github.com/FeiZhuLulu/Agent-Bridge/blob/main/src/agent_bridge/registry.py) определяет `runtime_context` при создании и показывает `runtime_context`/`dispatch_enabled` в статусе. Его методы отклоняют `dispatch_task`, `set_preferences`, `cancel_task`, `end_session` **до** проверки аргументов и мутаций. `user_requested=true` не обходит запрет.
4. [MCP tools](https://github.com/FeiZhuLulu/Agent-Bridge/blob/main/src/agent_bridge/server.py) и [правила координатора](https://github.com/FeiZhuLulu/Agent-Bridge/blob/main/ORCHESTRATION.md) объясняют отказ вызывающему. Это подсказка поверх проверки в коде, а не единственный механизм.

Это защита от случайной рекурсии через унаследованный MCP. Маркер окружения не является границей безопасности против worker, который намеренно удалит его или напрямую подключится к чужому локальному серверу. У оригинала также описан остаточный случай: host может очистить окружение MCP child.

## Где проходит граница у нас

`src/agent_shuttle/mcp_server.py` предоставляет `ask_agent`, `ask_codex`, `ask_antigravity`, `submit_task`, `cancel_task` и информационные операции. Синхронные вызовы идут через `BridgeClient`; длительные — через `TaskGateway` и управляемый A2A peer. `src/agent_shuttle/task_library.py:TaskManager` — самостоятельный Python API и источник задач/сессий для A2A. `src/agent_shuttle/client.py:BridgeClient` может посылать A2A запросы прямо в уже запущенный peer. Следовательно, одной проверки в MCP недостаточно.

Пути запуска worker: `backends.py` передает окружение Codex SDK и Antigravity CLI/SDK; `claude_runtime.py` и `opencode_runtime.py` формируют очищенные env. Все должны перенести маркер. `managed.py` наследует env при запуске A2A peer. Сейчас ни маркера, ни проверок runtime context нет. Текущая SQLite база библиотеки и MCP tickets расположены под `.agent-shuttle` в workspace; вложенный экземпляр не должен разделять их с координатором.

## Контракт для переноса

- Свой маркер `AGENT_SHUTTLE_PARENT_CONTEXT=worker` (не переиспользовать имя переменной другого установленного проекта), точное сравнение со значением `worker`. Вычислять роль при создании Bridge/manager и считать ее неизменной в течение жизни экземпляра. При формировании env worker ставить маркер **последним**, после настроек и фильтрации.
- В worker-контексте запрещены создание/отправка задачи (`ask*`, `submit_task`, `TaskManager.dispatch`, `dispatch_session`, `BridgeClient.submit/ask`), создание/привязка пользовательской сессии (`create_session`, `ensure_session`), явные `cancel_task`/`TaskManager.cancel`/`BridgeClient.cancel_task`, `end_session`/`close_session`, изменение preference. Запрет действует и для повторного `request_id`, и для `user_requested` аналога, если он появится.
- Чтение статуса, результата, транскрипта, каталога моделей и списка сессий допускается. Оно не должно в фоне открывать peer с правом отмены его задач. Для read-only MCP операций, которые сейчас вызывают `TaskGateway.handle()` и могут поднять peer, выбрать реализацию без мутаций или отказывать в worker-контексте до такого запуска.
- Разделить автоматически выбранные файлы вложенного экземпляра и координатора (`.agent-shuttle/nested/...`). При явном пути к базе/реестру в worker-контексте не открывать путь координатора для записи: либо принудительно направить в `nested/`, либо отклонить запуск с понятной ошибкой. Зафиксировать одно правило тестом. Это важно: открытие общей SQLite базы может обновить состояние прерванных задач уже при старте, до попытки dispatch.
- Использовать единый код ошибки `nested_dispatch_disabled` с понятным сообщением. Для Python — отдельное исключение `RuntimeError`/подкласс; для MCP — ошибка tool; для A2A — структурированный отказ без создания backend task. Точную форму ответа выбрать в первом красном transport-тесте и сохранить совместимость обычных ошибок.
- Служебная очистка собственных процессов и idle sessions остается возможной через внутренние методы. Публичный `end_session` в worker-контексте закрыт; janitor не должен вызывать его в обход проверок случайно. `TaskGateway.close()` не должен отменять задачи из прочитанного общего ticket registry.

## Циклы TDD

### 1. Красный: роль и окружение

В `tests/test_nested_context.py` проверить точное значение маркера, верхнеуровневый default и принудительное выставление после пользовательских env overrides. Параметризовать пути Codex, Antigravity CLI и SDK, Claude Code, OpenCode: проверить env перед реальным spawn через mock, без запуска harness. Проверить, что отсутствие маркера оставляет обычный worker launch и координацию доступными.

**Зеленый:** добавить один модуль `src/agent_shuttle/runtime_context.py` с константой, `is_worker_context()` и функцией маркировки env; применять ее в каждом месте запуска. У очищающих env runtime явно включить маркер в allowlist или поставить после очистки. Отдельно проверить `managed.py` при вложенном MCP.

### 2. Красный: библиотечный запрет до побочного эффекта

В `tests/test_library_tasks.py` создать manager с worker context и fake backend. Вызвать `dispatch`, повтор с `request_id`, `create_session`, `ensure_session`, `dispatch_session`, `set_preference`, `cancel`, `end_session`; ожидать один и тот же отказ до SQL INSERT/UPDATE, вызова backend и закрытия уже существующей native session. Проверить, что `status`, `result`, `list_tasks`, `list_sessions`, `get_preference` читаются, а coordinator выполняет прежние операции. Отдельно проверить служебную очистку при завершении manager и idle reaping.

**Зеленый:** центральный guard в `TaskManager` на публичных мутирующих входах; не полагаться на docstring или MCP tool policy. Вынести внутреннюю очистку из публичных методов там, где она должна выполняться независимо от роли.

### 3. Красный: A2A и прямой клиент

В `tests/test_a2a.py` и новом клиентском тесте отправить A2A `message/send` и `tasks/cancel` в worker-context сервер, вызвать `DELETE /bridge/sessions/{id}` и прямые `BridgeClient.submit/ask/cancel_task/close_session` из worker-контекста. Проверить отсутствие нового task, отсутствия отмены/закрытия существующих записей и структурированный отказ. Проверить верхнеуровневый round trip.

**Зеленый:** использовать guard `TaskManager` в A2A executor, добавить проверку перед A2A cancel handler и HTTP close route; guard в `BridgeClient` до сетевого обращения. Убедиться, что transport не переводит чужую задачу в `CANCELED` до проверки.

### 4. Красный: MCP через настоящий stdio процесс

Расширить `tests/test_mcp.py` и `tests/test_mcp_task_lifecycle.py`: запустить MCP subprocess с маркером и отдельным temporary workspace. Все `ask*`, `submit_task`, `cancel_task` отклоняются до `connect_harness`, `_gateway()` и записи ticket registry. Информационные операции имеют заявленное поведение. После остановки nested MCP исходная task, session и файл registry координатора не изменились. Снять маркер — существующий integration round trip проходит.

**Зеленый:** единая проверка в MCP mutating entrypoints и `TaskGateway.submit`; для вложенного процесса изолированный путь хранения. Обновить MCP instructions и RU/EN документацию о `runtime_context`, `dispatch_enabled`, отказе и ограничении env-маркера.

### 5. Регрессия и критерии приемки

Сначала запустить четыре новых группы по одной и увидеть ожидаемый красный результат, затем зеленый. После каждого цикла запускать затронутые тесты. Финальный локальный gate: `python -m unittest tests.test_nested_context tests.test_library_tasks tests.test_a2a tests.test_mcp tests.test_mcp_task_lifecycle -q`, затем весь `python -m unittest discover -s tests -q`. Для полноты проверить fake worker, который унаследовал MCP и пытается отправить задачу, при этом основная задача координатора завершается. Живые платные harness не требуются для приемки; если доступны, smoke на одном Codex и одном Antigravity подтверждает перенос env.

Готово, когда worker-инстанс не создает task/сессию и не меняет чужую при всех публичных входах, отказ наступает до сетевого/дискового побочного эффекта, а верхнеуровневые API и уборка собственных ресурсов сохраняют существующее поведение. Отдельная защита самого локального A2A порта от намеренного прямого клиента потребует аутентификации peer и остается самостоятельной задачей безопасности.
