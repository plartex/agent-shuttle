# Начало работы с Agent Shuttle

[English version / Английская версия](../getting-started.md)

Это руководство описывает процесс первоначальной настройки Agent Shuttle, проверку доступности локальных агентных сред (харнессов), запуск A2A- и MCP-серверов, а также отправку первых задач через Python, CLI и MCP.

---

## Требования

Agent Shuttle взаимодействует с локальными агентными средами разработки на вашем компьютере. Убедитесь в наличии:

- **uv** для рекомендуемой установки MCP-сервера и CLI ниже. uv может сам получить Python 3.11 или новее; отдельный Python требуется только для инструкций по работе из чекаута.
- Для **Codex**:
  - Установленное и авторизованное приложение Codex (десктоп или CLI).
  - Зависимость Python SDK (`openai-codex`) устанавливается автоматически вместе с Agent Shuttle.
- Для **Antigravity**:
  - Установленный и авторизованный CLI `agy` (интерактивно войдите через `agy`, затем проверьте командой `agy models`).
  - Проверьте авторизацию командой `agy models` в терминале.
- Для **OpenCode** (необязательно):
  - Установленный `opencode` (например, через npm: `npm i -g opencode-ai`).
  - Для локальных моделей: запущенная [Ollama](https://ollama.ai/) на `http://127.0.0.1:11434` с загруженной моделью (например, `ollama pull qwen3.5:9b`).
- Для **Claude Code** (необязательно):
  - Установленный CLI `claude` (`npm install -g @anthropic-ai/claude-code`).
  - Авторизованный аккаунт Anthropic либо настроенный локальный endpoint Ollama.

---

## Установка MCP-сервера или CLI

На Windows, macOS или Linux установите [uv](https://docs.astral.sh/uv/getting-started/installation/) и выполните:

```text
uv tool install agent-shuttle
uv tool dir --bin
```

Последняя команда выводит каталог с `agent-shuttle-mcp` (на Windows — `agent-shuttle-mcp.exe`). Укажите абсолютный путь к файлу в конфигурации MCP-клиента и используйте существующий инструмент `ask_agent`. При необходимости клиент сам запускает временный локальный A2A-сервер. Чтобы пользоваться `agent-shuttle` из терминала, при необходимости выполните `uv tool update-shell` и откройте новый терминал.

Для импорта из другого Python-проекта под управлением uv выполните в нём `uv add agent-shuttle`. Окружение `uv tool install` изолировано от импортов проекта.

Для диагностики поиска исполняемых файлов можно запустить `agent-shuttle discover`, а после входа в аккаунт — `agent-shuttle doctor <agent> --smoke` для проверки хода модели. Для самой установки эти команды не требуются.

---

## Настройка из чекаута репозитория (Windows, для разработки)

Для автономной разработки или тестирования непосредственно из репозитория:

```powershell
# 1. Клонирование репозитория
git clone https://github.com/Plartex/agent-shuttle.git
Set-Location agent-shuttle

# 2. Установка Python-пакета в отдельное окружение
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e .

# 3. Укажите MCP-клиенту абсолютный путь к
#    .venv\Scripts\agent-shuttle-mcp.exe

# 4. Откройте MCP-клиент и вызовите ask_codex или ask_antigravity.
# При необходимости A2A-сервер запустится автоматически на время запроса.
```

Antigravity Desktop может читать `%USERPROFILE%\.gemini\config\mcp_config.json` вместо локальной конфигурации MCP в чекауте. Если нужен постоянный A2A-сервер, запустите команду `agent-shuttle serve` ниже и остановите её сочетанием Ctrl+C.

---

## Установка из локального чекаута в другой проект

Вы можете установить Agent Shuttle в любой другой Python-проект без сохранения исходного чекаута репозитория:

```powershell
# В каталоге вашего проекта:
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Установка напрямую из каталога чекаута или готового wheel-файла
pip install C:\path\to\agent-shuttle
```

После установки в `.venv` проекта:
- Команда `agent-shuttle` доступна по пути `.venv\Scripts\agent-shuttle.exe`.
- Сервер MCP `agent-shuttle-mcp` доступен по пути `.venv\Scripts\agent-shuttle-mcp.exe`.
- Модули можно импортировать в Python: `from agent_shuttle import ShuttleClient, connect_harness, HarnessLaunch`.

---

## Проверка обнаружения харнессов (Discovery)

Перед запуском серверов проверьте, какие харнессы обнаружены в системе:

```powershell
agent-shuttle discover
```

Пример вывода:
```json
{
  "codex": "agent-shuttle",
  "antigravity": "C:\\Users\\username\\AppData\\Local\\agy\\bin\\agy.exe",
  "opencode": "C:\\Users\\username\\AppData\\Roaming\\npm\\node_modules\\opencode-ai\\bin\\opencode.exe",
  "claude_code": "C:\\Users\\username\\.local\\bin\\claude.exe"
}
```

Если исполняемый файл расположен вне стандартных путей поиска, укажите его явно:
```powershell
agent-shuttle discover --agy-command 'D:\tools\agy.exe'
# Или через переменную окружения:
$env:AGENT_SHUTTLE_AGY_COMMAND = 'D:\tools\agy.exe'
```

---

## Запуск серверов

### Сервер Codex
Запускает бэкенд Codex, привязанный к текущему каталогу проекта, на порту 8765:
```powershell
agent-shuttle serve codex --workspace . --port 8765
```

### Сервер Antigravity
Запускает бэкенд Antigravity CLI в headless-режиме на порту 8766:
```powershell
agent-shuttle serve antigravity --workspace . --port 8766
```

Запускайте команду в обычном сеансе Windows, где `agy models` видит ваш аккаунт.
Перед открытием HTTP-порта сервер проверяет каталог моделей через CLI без модельного
запроса. При отказе доступа к `%USERPROFILE%\.gemini\antigravity-cli` запуск
завершится явной ошибкой. Ограниченный клиент должен подключаться к серверу,
запущенному в авторизованном пользовательском сеансе.

### OpenCode или Claude Code (профиль Ollama)
OpenCode и Claude Code работают на основе JSON-профилей. Готовые примеры профилей находятся в каталоге `examples/`:

```powershell
# OpenCode с локальной Ollama
agent-shuttle serve profile --profile .\examples\opencode-ollama.json --workspace . --port 8767

# Claude Code с локальной Ollama
agent-shuttle serve profile --profile .\examples\claude-code-ollama.json --workspace . --port 8768
```

Параметр `--workspace` задает рабочий каталог агента и перекрывает относительное значение `workspace` из JSON-файла.

---

## Проверка статуса сервера

Каждый запущенный сервер предоставляет read-only эндпоинты HTTP на интерфейсе loopback:

- **Проверка идентификации** (мгновенная, без вызова языковых моделей):
  ```powershell
  Invoke-RestMethod http://127.0.0.1:8765/shuttle/identity
  ```
  Возвращает `{"agent": "codex", "backend": "codex_app_server", "workspace": "C:\\path\\to\\project", ...}`.

- **Возможности и квоты** (опрашивает метаданные харнесса):
  ```powershell
  agent-shuttle info http://127.0.0.1:8765
  ```

---

## Отправка первых задач

### Через CLI
```powershell
agent-shuttle ask http://127.0.0.1:8765 "Проверь тесты и перечисли упавшие."
```

С выбором модели и усилия рассуждений:
```powershell
agent-shuttle ask http://127.0.0.1:8766 "Объясни архитектуру репозитория." `
  --model gemini-3.8-flash-medium --reasoning-effort medium
```

### Через Python API
```python
import asyncio
from agent_shuttle import ShuttleClient

async def main():
    client = ShuttleClient()
    result = await client.ask(
        "http://127.0.0.1:8765",
        "Перечисли точки входа Python из pyproject.toml",
    )
    print("ID задачи:", result.task_id)
    print("Статус:", result.state)
    print("Ответ:\n", result.text)

asyncio.run(main())
```

### Через Model Context Protocol (MCP)
Добавьте Agent Shuttle в конфигурацию MCP-клиента. Для Antigravity Desktop проверьте `%USERPROFILE%\.gemini\config\mcp_config.json`: файл `.agents/mcp_config.json` в чекауте может не быть активной конфигурацией.

Пример конфигурации:
```toml
[mcp_servers.agent_shuttle]
command = "C:/path/to/agent-shuttle/.venv/Scripts/agent-shuttle-mcp.exe"
tool_timeout_sec = 1800
env_vars = ["AGENT_SHUTTLE_PARENT_CONTEXT"]

[mcp_servers.agent_shuttle.env]
AGENT_SHUTTLE_WORKSPACE = "C:/path/to/project"
```

Пример TOML подходит клиенту, который читает секции `mcp_servers`. Для клиента с JSON используйте те же значения `command` и `env` в его формате. Для MCP-запросов отдельно запускать `agent-shuttle serve` не нужно: при отсутствии подходящего сервера Agent Shuttle временно запускает его на время вызова. `AGENT_SHUTTLE_WORKSPACE` задаёт проверяемый проект.
Codex передаёт маркер worker унаследованному MCP child через `env_vars`. Если другой host очищает окружение MCP child, настройте в нём передачу `AGENT_SHUTTLE_PARENT_CONTEXT`.

После этого инструменты можно вызывать прямо в диалоге с агентом:
```text
ask_antigravity(prompt="Проверь тесты в tests/test_backends.py", model="gemini-3.8-flash-medium")
ask_codex(prompt="Выполни рефакторинг функции в src/agent_shuttle/discovery.py", model="gpt-5.6-terra")
```

---

## Дальнейшие шаги

- Изучите полный [Справочник API](api.md) для программного управления серверами и сессиями.
- Ознакомьтесь с [Руководством по безопасности и разрешениям](permissions.md) перед включением `full_access`.
- Узнайте подробнее о внутреннем устройстве и протоколе в разделе [Архитектура](architecture.md).
- В случае затруднений обратитесь к разделу [Диагностика и устранение неполадок](troubleshooting.md).
