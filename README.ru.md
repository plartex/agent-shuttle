# Agent Shuttle

[![Тесты](https://github.com/Plartex/agent-shuttle/actions/workflows/tests.yml/badge.svg)](https://github.com/Plartex/agent-shuttle/actions/workflows/tests.yml)
[![Лицензия MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![A2A Protocol 1.0](https://img.shields.io/badge/A2A-1.0_JSON--RPC-blue)](https://a2a-protocol.org/latest/)
[![MCP](https://img.shields.io/badge/MCP-tools-green)](https://modelcontextprotocol.io/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

[English version / Английская версия](README.md)

Agent Shuttle даёт Python-приложениям единый способ работать с **Codex**, **Antigravity**, **OpenCode**, **Claude Code** и агентами **ACP из профилей**. Библиотека запускает локальные задачи агентов, сохраняет многошаговые сессии и предоставляет доступ через [A2A 1.0 JSON-RPC](https://a2a-protocol.org/latest/) и [MCP](https://modelcontextprotocol.io/).

Библиотека позволяет агентам и внешним программам делегировать задачи агентам-партнерам, повторно использовать контекст многошаговых бесед, запрашивать актуальные каталоги моделей и квоты аккаунтов, а также видеть гарантии политик доступа каждого runtime — исключительно на локальном интерфейсе (`127.0.0.1`) без передачи облачных API-ключей.

---

## Поддерживаемые агентные среды (харнессы)

| Харнесс | Основной механизм интеграции | Авторизация и доступ к моделям | Ключевые возможности |
|---|---|---|---|
| **Codex** | Официальный Python SDK `openai-codex` | Авторизация локального Codex App Server | Песочницы (`workspace_write`, `read_only`, `full_access`), каталог моделей и усилий рассуждения, чтение квот через `account/rateLimits/read`. |
| **Antigravity** | Официальный CLI `agy` в headless-режиме (`-p` / `stream-json`) | Авторизованный аккаунт Antigravity | Считывание моделей, усилий и квот `/usage` в реальном времени. Режим стандартных настроек либо полный доступ (`--dangerously-skip-permissions`). Опциональный устаревший SDK backend. |
| **OpenCode** | Управляемый локальный HTTP-сервер (`--pure serve`) | Локальная Ollama или сторонние провайдеры | Настройка через JSON-профиль, гранулярные политики инструментов (`no_tools`, `read_only`, `workspace_write`, `full_access`), варианты моделей. |
| **Claude Code** | Управляемый CLI в print-режиме (`claude -p`) | Локальный endpoint Ollama или Anthropic | Настройка через JSON-профиль, изолированные временные конфигурации сессий, возобновление диалога, безопасный режим против полного доступа. |
| **Агент ACP из профиля** | Agent Client Protocol через локальный stdio | Учётная запись и настройки самого агента | Регистрация команды в JSON-профиле, поток событий, отмена и возобновление сессий при объявленной возможности `session/load`. Политики доступа имеют рекомендательный характер. |

---

## Установка

Agent Shuttle работает на Windows, Linux и macOS. Для командной утилиты и MCP-сервера сначала установите [uv](https://docs.astral.sh/uv/getting-started/installation/); uv может сам получить подходящий Python.

### Установка команд и MCP-сервера

```text
uv tool install git+https://github.com/Plartex/agent-shuttle.git
uv tool dir --bin
```

Пакет пока не опубликован в PyPI. Последняя команда показывает каталог с `agent-shuttle-mcp` (на Windows — с расширением `.exe`); абсолютный путь к нему укажите в конфигурации MCP-клиента. Чтобы пользоваться `agent-shuttle` из терминала, при необходимости выполните `uv tool update-shell` и откройте новый терминал. После проверенной публикации в PyPI команда установки станет `uv tool install agent-shuttle`.

Для диагностики локальных харнессов можно запустить `agent-shuttle discover`: команда не проверяет авторизацию и не обращается к модели. `agent-shuttle doctor <agent> --smoke` при необходимости проверяет реальный ход модели. В MCP уже есть `ask_agent` для поддерживаемых харнессов и настроенных профилей.

### Использование как Python-библиотеки в другом проекте

```text
uv add git+https://github.com/Plartex/agent-shuttle.git
```

`uv tool install` устанавливает инструмент отдельно от Python-окружения проекта. Если ваш код импортирует `agent_shuttle`, добавьте пакет как зависимость проекта. После проверенной публикации в PyPI используйте `uv add agent-shuttle`.

### Установка из локального чекаута

Библиотеку можно установить напрямую из каталога репозитория в виртуальное окружение вашего проекта:

```powershell
# Создание и активация виртуального окружения
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Установка в стандартном или редактируемом режиме
pip install C:\path\to\agent-shuttle
# или редактируемый режим для разработки:
# pip install -e C:\path\to\agent-shuttle
```

После установки консольные утилиты (`agent-shuttle`, `agent-shuttle-mcp`) и Python API (`agent_shuttle`) становятся доступны в этом окружении. Исходный каталог репозитория для последующей работы клиентских проектов не требуется. Версия 0.6 удаляет старые импорты `agent_bridge` и команды `agent-bridge`; ключи метаданных A2A `agent_bridge.*` остаются частью протокола.

В дистрибутиве один Python-пакет: `src/agent_shuttle/`. Это стандартная структура `src`; второго пакета с реализацией или совместимыми импортами нет.

---

## Быстрый старт

Установите MCP-сервер и получите каталог его исполняемого файла:

```text
uv tool install git+https://github.com/Plartex/agent-shuttle.git
uv tool dir --bin
```

В конфигурации MCP-клиента укажите абсолютный путь к `agent-shuttle-mcp` из выведенного каталога. При запросе MCP сам запускает локальный A2A-сервер, если тот не работает. Пример настройки и сведения о харнессах есть в [руководстве](docs/ru/getting-started.md).

Если нужен постоянный сервер, запустите `agent-shuttle serve codex --workspace . --port 8765` (или `serve antigravity` на порту 8766) в терминале и остановите его сочетанием Ctrl+C.

---

## Минимальные примеры

### 1. Python API

```python
import asyncio
from pathlib import Path
from agent_shuttle import ShuttleClient, HarnessLaunch, connect_harness

async def main():
    client = ShuttleClient()

    # Запрос каталога моделей и информации о квотах в реальном времени
    info = await client.info("http://127.0.0.1:8766")
    print("Выбранная модель:", info["capabilities"]["selected_model"])

    # Одиночный вызов агента
    result = await client.ask(
        "http://127.0.0.1:8766",
        "Объясни структуру проекта и перечисли точки входа.",
        model="gemini-3.8-flash-medium",
        reasoning_effort="medium",
    )
    print(f"[{result.state}] Задача {result.task_id}:\n{result.text}")

    # Повторно используемая сессия (сохраняет контекст диалога)
    async with client.session(
        "http://127.0.0.1:8765",
        model="gpt-5.6-terra",
        reasoning_effort="high",
    ) as session:
        step1 = await session.ask("Какие миграции базы данных еще не применены?")
        step2 = await session.ask("Сгенерируй SQL для применения первой из них.")
        print("Ответ второго шага:", step2.text)
        print("Расход токенов второго шага:", step2.usage)

    # Управление жизненным циклом: переиспользование существующего или запуск временного сервера
    launch = HarnessLaunch(
        name="antigravity",
        url="http://127.0.0.1:8766",
        workspace=Path.cwd(),
    )
    async with connect_harness(launch) as conn:
        print("Подключено к:", conn.url, "(запущен временный сервер:", conn.started, ")")

asyncio.run(main())
```

### 2. Интерфейс командной строки (CLI)

```powershell
# Поиск локально установленных харнессов без запуска моделей
agent-shuttle discover

# Проверка установки и подключения без хода модели
agent-shuttle doctor
agent-shuttle doctor --profile .\examples\opencode-ollama.json

# После исправления ошибки — один пробный ход с расходом токенов
agent-shuttle doctor codex --smoke

# Запуск A2A-сервера для Codex
agent-shuttle serve codex --workspace . --port 8765

# Запуск A2A-сервера для Antigravity (в режиме CLI)
agent-shuttle serve antigravity --workspace . --port 8766

# Запуск A2A-сервера из профиля (OpenCode, Claude Code или ACP)
agent-shuttle serve profile --profile .\examples\opencode-ollama.json --workspace . --port 8767

# Запрос возможностей сервера, каталога моделей и лимитов квот
agent-shuttle info http://127.0.0.1:8765

# Отправка задачи из командной строки
agent-shuttle ask http://127.0.0.1:8765 "Сделай краткое резюме последних изменений" --model gpt-5.6-terra
```

`doctor` отдельно показывает проверку установки, подключения и реального хода. Без аргументов он проверяет встроенные агенты и локальные профили из `BRIDGE_AGENTS_JSON`; отсутствующие необязательные агенты пропускаются. Для проверки подключения OpenCode и Claude Code нужен JSON-профиль. `--json` выводит те же данные для скриптов. Коды выхода: `0` — обязательные проверки прошли, `1` — цель или пробный ход не прошли, `2` — неверные аргументы или конфигурация. `--smoke` требует одну цель и использует обеспеченный `no_tools` (для Codex — `read_only`). ACP-проба отклоняется, поскольку ACP не гарантирует эти ограничения.

### Регистрация агента ACP

Скопируйте [`examples/acp-worker.json`](examples/acp-worker.json), укажите команду запуска установленного агента в массиве `command` и рабочую папку `workspace`. Оболочка для запуска команды не используется. Поля `provider`, `default_model`, `allowed_models` и `reasoning_efforts` необязательны. Без списков разрешённых вариантов можно явно выбрать любую модель или усилие, объявленные агентом в ACP config options; заданные списки сужают этот выбор.

```powershell
agent-shuttle discover --profile .\examples\acp-worker.json
agent-shuttle serve profile --profile .\examples\acp-worker.json --port 8768
agent-shuttle info http://127.0.0.1:8768
agent-shuttle ask http://127.0.0.1:8768 "Объясни этот проект" --tool-policy read_only
```

Для управляемого запуска через MCP можно связать произвольный `agent_id` с профилем через `BRIDGE_AGENTS_JSON`: `{"my-acp": {"harness": "acp", "profile": "C:/profiles/my-acp.json"}}`.

`discover` проверяет команду без запуска агента. `info` выполняет ACP handshake и показывает объявленные возможности без модельного запроса. Для ACP политики доступа **не гарантируются**: результат содержит предупреждение, а `read_only_tools` остаётся `false`. При необходимости строгих ограничений используйте отдельно проверенную изоляцию.

### 3. Model Context Protocol (MCP)

Запуск stdio-сервера MCP:
```powershell
agent-shuttle-mcp
# или: python -m agent_shuttle.mcp_server
```

Доступные инструменты MCP:
- `ask_agent(agent_id, prompt, model?, reasoning_effort?, tool_policy?, workspace?)`: Использует подходящий локальный A2A-сервер или временно запускает его. Встроенные идентификаторы: `codex`, `antigravity`, `opencode`, `claude_code`; настраиваемым ACP-агентам нужен JSON-профиль в `BRIDGE_AGENTS_JSON`.
- `get_agent_info(agent_id, workspace?)`: Возвращает модели и квоты, при необходимости запуская временный сервер.
- `ask_antigravity(prompt, model?, reasoning_effort?, workspace?, tool_policy?, turn_timeout_seconds=300)`: При необходимости запускает сервер Antigravity.
- `ask_codex(prompt, model?, reasoning_effort?, workspace?)`: При необходимости запускает сервер Codex.
- `get_antigravity_info(workspace?)` и `get_codex_info()`: Читают возможности и квоты без расхода модельных запросов.

---

## Ключевые концепции

### Обнаружение харнессов (Discovery)
Команда `agent-shuttle discover` (или вызов `discover_harnesses()` в Python) проверяет установленные исполняемые файлы без старта процессов и загрузки весов. Проверяется переменная `PATH` и стандартные каталоги установки операционной системы (`%LOCALAPPDATA%\agy\bin`, глобальные пути npm и т.д.). Пути можно переопределить переменными окружения (`BRIDGE_AGY_COMMAND`) или флагами CLI (`--agy-command`, `--opencode-command`, `--claude-command`).

### Выбор модели и уровня рассуждений (Reasoning Effort)
Параметры передаются через метаданные A2A (`agent_bridge.model`, `agent_bridge.reasoning_effort`):
- **Antigravity:** Уровень рассуждений встроен в идентификаторы моделей (например, `gemini-3.8-flash-medium`). При одновременной передаче `--model` и `--effort` значения обязаны совпадать.
- **Codex:** Модель и усилие рассуждения настраиваются независимо согласно каталогу, возвращаемому `get_codex_info`.
- **OpenCode и Claude Code:** Профили определяют списки `allowed_models` и допустимые `reasoning_efforts` (например, варианты модели для Ollama или флаги CLI).

### Рабочие каталоги (Workspaces) и изоляция сессий
- Каждый сервер привязывается к строго проверенному каноническому каталогу проекта.
- `connect_harness()` сверяет рабочий каталог существующего сервера с запрошенным перед его повторным использованием.
- **Сессии:** Объект `BridgeSession` поддерживает непрерывную беседу на протяжении нескольких вызовов `ask()`. Параметры сессии (модель, reasoning effort, политика инструментов) фиксируются при открытии и не могут изменяться между шагами. Неактивные сессии автоматически закрываются через 30 минут бездействия.
- **Задачи на уровне библиотеки:** `TaskManager` запускает агента напрямую из Python, без A2A или MCP. Он управляет ID задач, сессиями, SQLite-журналом событий, отменой, страницами результата и восстановлением после сбоя. A2A показывает те же ID и результаты. Подробнее — в [справочнике Python API](docs/ru/api.md#taskmanager-задачи-на-уровне-библиотеки).
- **Удалённые задачи:** `ShuttleClient.submit()` сразу возвращает `TaskHandle`. Доступны `status()`, ограниченное по времени `wait(timeout)`, `events()`, `result_page()`, `transcript()`, `result()` и `cancel()`; к задаче можно вернуться по ID через `client.task(url, task_id)`. Истечение ожидания не останавливает агента. Необязательный UUID `request_id` защищает от повторного запуска. При включённом `--task-db` завершённые задачи переживают перезапуск, а прерванные получают ошибку без повторного выполнения. Подробнее — в [справочнике API](docs/ru/api.md#taskhandle-и-bridgeevent).

### Безопасность и политики инструментов (Tool Policies)
Agent Shuttle определяет четыре стандартные политики инструментов:
- `no_tools`: Полный запрет вызова инструментов.
- `read_only`: Разрешены операции поиска и чтения файлов без модификации.
- `workspace_write`: Разрешена модификация файлов внутри рабочего каталога.
- `full_access`: Явное снятие всех ограничений инструментов и запросов подтверждения.

Для агентов ACP из профилей эти политики носят рекомендательный характер. Agent Shuttle проверяет запросы разрешений ACP, но не может запретить агенту действия вне таких запросов. Подробнее — в [руководстве по правам](docs/ru/permissions.md#5-агент-acp-из-профиля).

> [!WARNING]
> `full_access` даёт агенту неограниченный доступ к инструментам для конкретной задачи. Флаг `--agy-dangerously-skip-permissions` делает этот режим доступным на сервере Antigravity. Оставляйте серверы на loopback. Явная политика `read_only` в Antigravity применяется через проверенный хук `PreToolUse`; политика CLI по умолчанию не даёт такой гарантии.

---

## Тестирование

Agent Shuttle содержит полный набор офлайн-тестов на базе имитационных (fake) бэкендов, не требующих доступа к сети, учетных записей или расхода квот:

GitHub Actions запускает один и тот же набор на Windows, Ubuntu Linux и macOS с Apple Silicon (`macos-15`) для Python 3.11 и 3.12. Процессный smoke использует только локальный Python worker и проверяет очистку дерева процессов; агентские CLI и учётные записи не нужны.

```powershell
python -m unittest discover -s tests -v
```

Интеграционные тесты с реальными моделями остаются отдельными проверками по запросу и не входят в шесть заданий CI. Они запускаются при наличии соответствующего окружения (например, `BRIDGE_LIVE_OLLAMA_MODEL=qwen3.5:9b` или `BRIDGE_LIVE_AGY_FULL_ACCESS=1`). Подробные инструкции приведены в [CONTRIBUTING.ru.md](CONTRIBUTING.ru.md).

---

## Разделы документации

- [Руководство по началу работы](docs/ru/getting-started.md)
- [Справочник API](docs/ru/api.md)
- [Разрешения и безопасность](docs/ru/permissions.md)
- [Архитектура и протокол](docs/ru/architecture.md)
- [Диагностика и устранение неполадок](docs/ru/troubleshooting.md)
- [Участие в разработке](CONTRIBUTING.ru.md)
- [Политика безопасности](SECURITY.ru.md)
