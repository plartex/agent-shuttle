# Codex ↔ Antigravity Agent Bridge

Локальный двусторонний мост между harness-агентами OpenAI Codex и Google Antigravity.

- **A2A 1.0 JSON-RPC** между самостоятельными агентами.
- **MCP tools** для вызова второго агента из Codex или Antigravity.
- **Python API** для приложений и автоматизации.
- Выбор модели при каждом запросе.
- Отдельный интерфейс моделей, reasoning effort и текущих квот аккаунта.
- Основной режим использует вход в аккаунты Codex и Antigravity, а не ключи LLM API.

> Статус: alpha. Мост предназначен для локальной разработки и слушает только `127.0.0.1`.

## Архитектура

```text
Codex ── MCP ask_antigravity ── A2A ── agy CLI ── Antigravity harness/tools
Antigravity ── MCP ask_codex ── A2A ── Codex SDK ── Codex harness/tools
Application ── BridgeClient/HTTP ── A2A servers
```

Antigravity запускается через официальный `agy` CLI в headless режиме. Google указывает, что CLI использует общий агентный harness Antigravity и сохранённую авторизацию аккаунта. Codex запускается через официальный Python SDK, который управляет локальным Codex App Server.

## Быстрый старт на Windows

Требования:

- Python 3.11+;
- установленный и авторизованный Codex;
- установленный и авторизованный [`agy` CLI](https://antigravity.google/docs/cli/install/).

```powershell
git clone https://gitlab.com/kkaastr/codex-antigravity-a2a-bridge.git
Set-Location codex-antigravity-a2a-bridge
& .\Install.ps1
& .\Configure-Mcp.ps1
& .\Start-Bridge.ps1
```

Если Python установлен вне `PATH`, перед установкой задайте `BRIDGE_BOOTSTRAP_PYTHON` с полным путём к `python.exe`.

Если `agy.exe` отсутствует в `PATH`, задайте путь перед запуском:

```powershell
$env:BRIDGE_AGY_COMMAND = 'C:\path\to\agy.exe'
& .\Start-Bridge.ps1
```

Скрипт запуска поднимает:

| Agent | A2A endpoint | Agent card |
|---|---|---|
| Codex | `http://127.0.0.1:8765` | `http://127.0.0.1:8765/.well-known/agent-card.json` |
| Antigravity | `http://127.0.0.1:8766` | `http://127.0.0.1:8766/.well-known/agent-card.json` |

Остановка: `& .\Stop-Bridge.ps1`. Логи и PID-файлы находятся в `.runtime/` и не попадают в Git.

## MCP инструменты

После `Configure-Mcp.ps1` появляются локальные конфигурации `.codex/config.toml` и `.agents/mcp_config.json` с абсолютным путём к Python окружению.

| Tool | Назначение |
|---|---|
| `ask_antigravity(prompt, model?)` | Поставить задачу Antigravity |
| `ask_codex(prompt, model?)` | Поставить задачу Codex |
| `get_antigravity_info()` | Получить модели, effort и квоты Antigravity |
| `get_codex_info()` | Получить модели, effort и квоты Codex |
| `evaluate(target_type, target, ...)` | Проверить сниппет, файл или проект пакетами LLM-правил |

```text
ask_antigravity(prompt="Проверь тесты", model="gemini-3.8-flash-medium")
ask_codex(prompt="Проверь тесты", model="gpt-5.6-terra")
```

## Проверка качества кода

Встроенный профиль `code_smells` содержит 80 правил. LLM-агент выполняет их пакетами, а мост локально проверяет структуру ответа, агрегирует результаты и рассчитывает итоговую оценку. Анализ только выявляет запахи: он не предлагает исправления и не изменяет код.

Цель всегда задаётся явно:

```powershell
# Фрагмент кода
& .\.venv\Scripts\python.exe -m agent_bridge.cli evaluate snippet `
  --code 'def process_order(): ...' --language python

# Один файл
& .\.venv\Scripts\python.exe -m agent_bridge.cli evaluate file .\src\service.py `
  --provider agent-bridge:codex

# Проект целиком, JSON-отчёт
& .\.venv\Scripts\python.exe -m agent_bridge.cli evaluate project .\ `
  --provider agent-bridge:antigravity --batch-size 10 --json --output report.json
```

Можно выбрать отдельные правила:

```powershell
& .\.venv\Scripts\python.exe -m agent_bridge.cli evaluate file .\src\service.py `
  --rules long_method,large_class,duplicate_code
```

Результат отдельной проверки имеет один из статусов: `passed`, `failed`, `skipped`, `inconclusive` или `error`. Для `failed` агент обязан вернуть файл, строки и доказательство; ссылки за пределами переданной цели отклоняются. Невалидный ответ один раз отправляется агенту на исправление.

Текстовый итог выглядит так:

```text
Code Smells: 72/80 PASSED
FAILED: 5 · SKIPPED: 2 · INCONCLUSIVE: 1 · ERRORS: 0
Code quality: 87.3% · Assessment coverage: 96.2%
```

Python API:

```python
from agent_bridge.evaluation import EvaluationService, EvaluationTarget, load_code_smells_profile
from agent_bridge.evaluation.providers import AgentBridgeProvider

provider = AgentBridgeProvider("http://127.0.0.1:8765", "codex")
report = await EvaluationService(provider).evaluate(
    EvaluationTarget.project("D:/projects/example"),
    load_code_smells_profile(),
    batch_size=10,
)
print(report.to_json())
```

Codex-запросы evaluation запускаются с read-only sandbox. Текущий Antigravity CLI не предоставляет мосту эквивалентной гарантии, поэтому отчёт содержит предупреждение о режиме доступа.

Если Antigravity CLI не обнаруживает `.agents/mcp_config.json`, зарегистрируйте stdio-сервер через `agy mcp add`. Для headless вызовов разрешите конкретные инструменты правилами вида `mcp(agent-bridge/ask_codex)` в `~/.gemini/antigravity-cli/settings.json`. Формат правил описан в [документации Antigravity](https://antigravity.google/docs/cli/permissions/).

## Выбор модели

Поле `model` проходит в `message.metadata["agent_bridge.model"]` запроса A2A. Сервер передаёт его в `agy --model` или `Codex.thread_start(model=...)`.

Это соглашение данного моста поверх расширяемых metadata A2A. Если `model` не указан, каждый harness использует собственную текущую настройку. Упоминание модели только внутри `prompt` не переключает модель запуска.

Доступные ID следует получать из интерфейса возможностей или самого harness. Antigravity отклоняет неизвестный model ID. Каталог Codex содержит поддерживаемые reasoning efforts для каждой модели.

## Модели, effort и usage

Каждый сервер предоставляет read-only HTTP интерфейс:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/bridge/info
Invoke-RestMethod http://127.0.0.1:8766/bridge/info

Invoke-RestMethod http://127.0.0.1:8765/bridge/capabilities
Invoke-RestMethod http://127.0.0.1:8766/bridge/usage
```

- `/bridge/info` — полный снимок;
- `/bridge/capabilities` — выбранная модель, effort и каталог моделей;
- `/bridge/usage` — группы квот, использованный и оставшийся процент, окно и время сброса.

Данные читаются при каждом запросе из harness без запуска агентной задачи:

- Codex App Server: `model/list`, `config/read`, `account/rateLimits/read`;
- Antigravity CLI: `models`, `/model`, `/effort`, `/usage`.

Google сообщает, что read-only slash commands в print mode не расходуют модельную квоту. Antigravity возвращает общий доступный набор effort, но не таблицу совместимости по каждой модели. Codex возвращает усилия отдельно для каждой модели.

## Python API

```python
import asyncio
from agent_bridge import BridgeClient


async def main():
    client = BridgeClient()
    info = await client.info("http://127.0.0.1:8766")
    print(info["capabilities"]["selected_model"])

    result = await client.ask(
        "http://127.0.0.1:8766",
        "Проверь проект и предложи исправление ошибок тестов",
        model="gemini-3.8-flash-medium",
    )
    print(result.state, result.task_id, result.text)


asyncio.run(main())
```

CLI:

```powershell
& .\.venv\Scripts\python.exe -m agent_bridge.cli info http://127.0.0.1:8765
& .\.venv\Scripts\python.exe -m agent_bridge.cli ask http://127.0.0.1:8766 'Проверь проект' --model gemini-3.8-flash-medium
```

## Необязательный Antigravity SDK backend

SDK backend устанавливается отдельно из-за несовместимых версий Protobuf:

```powershell
$env:BRIDGE_INSTALL_AGY_SDK = '1'
& .\Install.ps1
$env:BRIDGE_AGY_MODE = 'sdk'
& .\Start-Bridge.ps1
```

Основной сценарий с аккаунтом и квотами Antigravity использует `agy` CLI. SDK backend может иметь другую схему авторизации и не предоставляет каталог или квоты CLI; info endpoint вернёт `available: false`.

## Тесты

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s .\tests -v
```

Тесты используют fake backend и не расходуют лимиты моделей. GitLab CI выполняет компиляцию пакета и тот же набор тестов.

## Ограничения и безопасность

- Каждый `ask` создаёт новую сессию агента; открытый диалог IDE не продолжается.
- Передаются текстовая задача и ответ. Общие файлы доступны агентам через workspace.
- A2A task store находится в памяти и очищается при перезапуске.
- Серверы не имеют сетевой аутентификации. Не публикуйте порты 8765/8766 без TLS и auth.
- Инструменты агента подчиняются sandbox и permission policy соответствующего harness.
- Usage endpoint раскрывает локальным процессам состояние квот аккаунта.
- `config/read` и `account/rateLimits/read` вызываются через типизированный низкоуровневый транспорт Codex SDK, пока высокоуровневый Python API не предоставляет их напрямую.

См. [SECURITY.md](SECURITY.md) и [CONTRIBUTING.md](CONTRIBUTING.md).

## Документация

- [Проект LLM-first evaluation framework](docs/evaluation-framework-design.md)
- [A2A Protocol](https://a2a-protocol.org/latest/)
- [Antigravity CLI overview](https://antigravity.google/docs/cli/overview/)
- [Antigravity headless mode](https://antigravity.google/docs/cli/headless/)
- [Antigravity MCP](https://antigravity.google/docs/mcp/)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
