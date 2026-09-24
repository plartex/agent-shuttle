# Agent Bridge: Codex, Antigravity, OpenCode и Claude Code

Локальный A2A/MCP-мост между harness-агентами Codex, Antigravity, OpenCode и Claude Code. OpenCode и Claude Code поддерживают локальные модели Ollama; другие провайдеры задаются серверным профилем.

- **A2A 1.0 JSON-RPC** между самостоятельными агентами.
- **MCP tools** для вызова второго агента из Codex или Antigravity.
- **Python API** для приложений и автоматизации.
- Повторно используемые сессии Python API: несколько задач в одной беседе Codex или Antigravity.
- Выбор модели при каждом запросе.
- Отдельный интерфейс моделей, reasoning effort и текущих квот аккаунта.
- Основной режим использует вход в аккаунты Codex и Antigravity, а не ключи LLM API.

## Использование в другом проекте

Установите Agent Bridge в Python-окружение проекта из исходного репозитория или wheel-файла. После установки исходный checkout не требуется: CLI и Python API импортируются из окружения проекта. Например, в PowerShell из каталога своего проекта:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install 'C:\path\to\agent-bridge'
& .\.venv\Scripts\agent-bridge.exe serve codex --workspace . --port 8765
```

Для Antigravity используйте `serve antigravity --workspace . --port 8766`. Для OpenCode и Claude Code создайте профиль по [примерам](examples), затем запустите `serve profile --profile <путь-к-JSON> --workspace . --port <порт>`. Параметр `--workspace` задаёт рабочий каталог агента и перекрывает `workspace` из профиля. Если его не передать, относительный путь внутри JSON считается от каталога профиля. ID модели и доступные политики инструментов задаются профилем; сервер слушает только `127.0.0.1`.

Для приложения-клиента достаточно `BridgeClient().info(url)` и `BridgeClient().ask(url, prompt, model=...)`. Agent Bridge не содержит правил code smells и может использоваться отдельно от чекера.

`agent-bridge discover` (или `discover_harnesses()` в Python) показывает доступные локальные харнессы без запуска серверов и моделей. Поиск проверяет `PATH` и типовые пользовательские каталоги установки на Windows; для `agy.exe` также учитываются `BRIDGE_AGY_COMMAND`, `%LOCALAPPDATA%\agy\bin` и `bin` рядом с исходным checkout Agent Bridge. Пути можно переопределить флагами `--agy-command`, `--opencode-command`, `--claude-command` или аргументом `discover_harnesses({"opencode": "C:/tools/opencode.exe"})`. Обнаружение не означает, что Ollama уже запущена или нужная модель загружена.

## Профили OpenCode и Claude Code

Новые runtime подключаются через серверный JSON-профиль, а не через отдельный класс для каждого поставщика модели. Установите OpenCode либо Claude Code и запустите Ollama с моделью, указанной в профиле. Примеры [OpenCode](examples/opencode-ollama.json) и [Claude Code](examples/claude-code-ollama.json) используют `qwen3.5:9b`; замените ID на свою установленную модель. Относительный `workspace` считается от каталога JSON-файла.

```powershell
# Два независимых локальных A2A-сервера; запускать в разных терминалах:
& .\.venv\Scripts\agent-bridge.exe serve profile --profile .\examples\opencode-ollama.json --workspace . --port 8767
& .\.venv\Scripts\agent-bridge.exe serve profile --profile .\examples\claude-code-ollama.json --workspace . --port 8768

# Отправка задачи в любой из них:
& .\.venv\Scripts\agent-bridge.exe ask http://127.0.0.1:8767 "Ответь одним словом: OK" --model qwen3.5:9b --reasoning-effort none --tool-policy no_tools
& .\.venv\Scripts\agent-bridge.exe ask http://127.0.0.1:8768 "Ответь одним словом: OK" --model qwen3.5:9b --tool-policy no_tools
```

`agent-bridge info URL` возвращает разрешённые модели, усилия рассуждения и максимальную политику инструментов. `no_tools` запрещает инструменты; `read_only` разрешает чтение/поиск; `workspace_write` включает модификацию проекта и потому требует явного разрешения в профиле. Запрос не может расширить `max_tool_policy`, заменить endpoint либо передать секрет. Политики инструментов — ограничение интерфейса агента, **не OS-песочница**: не запускайте недоверенный код без системной изоляции.

Для OpenCode кроме Ollama доступны встроенные провайдеры OpenCode: задайте `provider`, разрешённые `allowed_models` и нужный `credential_env` в профиле, а аутентификацию настройте в самом runtime. Для Claude Code поддержаны стандартный Anthropic и Ollama-совместимый endpoint. Совместимость конкретного стороннего провайдера/модели нужно проверять отдельно; наличие ID в профиле не гарантирует поддержку runtime. `reasoning_efforts` — явный allowlist профиля; Ollama/OpenCode использует варианты модели, Claude Code передаёт поддерживаемый effort в CLI.

Для MCP задайте `BRIDGE_AGENTS_JSON` как словарь ID→локальный URL, например `{"opencode-local":"http://127.0.0.1:8767","claude-local":"http://127.0.0.1:8768"}`. Инструменты `ask_agent(agent_id, prompt, model?, reasoning_effort?, tool_policy?)` и `get_agent_info(agent_id)` работают с любым таким профилем. Старые `ask_codex` и `ask_antigravity` сохранены.

Python API также сохраняет сессию для нескольких ходов: `AgentProfile.from_file(path)`, затем `backend, info = build_profile(profile)`, `session = await backend.open_session(...)`, `await session.ask(...)`, `await session.close()`, `await backend.close()`. Новая сессия фиксирует модель, effort и политику инструментов; менять их между ходами нельзя. Секреты храните в переменных окружения, не в JSON.

Локальные интеграционные тесты по умолчанию пропускаются. Для проверки обеих связок установите `BRIDGE_LIVE_OLLAMA_MODEL` в ID установленной модели и запустите `python -m unittest tests.test_live_ollama -v`; при необходимости укажите абсолютные пути в `BRIDGE_LIVE_OPENCODE_COMMAND` и `BRIDGE_LIVE_CLAUDE_COMMAND`.

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
| `ask_antigravity(prompt, model?, reasoning_effort?)` | Поставить задачу Antigravity |
| `ask_codex(prompt, model?, reasoning_effort?)` | Поставить задачу Codex |
| `get_antigravity_info()` | Получить модели, effort и квоты Antigravity |
| `get_codex_info()` | Получить модели, effort и квоты Codex |

```text
ask_antigravity(prompt="Проверь тесты", model="gemini-3.8-flash-medium", reasoning_effort="medium")
ask_codex(prompt="Проверь тесты", model="gpt-5.6-terra", reasoning_effort="high")
```

Проверка качества кода вынесена в отдельную библиотеку `agent-code-checker`.
Она использует Agent Bridge как зависимость; сам Bridge не содержит правил и оценок.

## Выбор модели

Поля `model` и `reasoning_effort` проходят в metadata A2A как
`agent_bridge.model` и `agent_bridge.reasoning_effort`. Сервер передаёт их в
`agy --model/--effort` либо в `Codex.thread_start(model=..., config={"model_reasoning_effort": ...})`.

Это соглашение данного моста поверх расширяемых metadata A2A. Если параметры не указаны, каждый harness использует собственные текущие настройки. Упоминание модели или effort только внутри `prompt` не переключает настройки запуска.

В актуальном каталоге Antigravity effort входит и в model ID (`...-low`,
`...-medium`, `...-high`). При одновременной передаче `--model` и
`--reasoning-effort` значения должны совпадать; `agy` отклоняет противоречивую
пару. У Codex модель и reasoning effort задаются независимо в пределах
совместимости, которую возвращает `get_codex_info`.

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

Для связанных задач используйте одну сессию. `async with` закрывает её даже при
ошибке; каждый `ask()` возвращает обычный `BridgeResult` со своим A2A task ID.
Модель, effort и режим `read_only` фиксируются на всю сессию.

```python
import asyncio
from agent_bridge import BridgeClient

async def main():
    bridge = BridgeClient()
    async with bridge.session(
        "http://127.0.0.1:8766",
        model="gemini-3.8-flash-medium",
        reasoning_effort="medium",
    ) as session:
        first = await session.ask("Объясни коротко, что такое RLS.")
        second = await session.ask("Теперь назови риск из предыдущего ответа.")
        print(first.text, second.text)
        print(second.usage)  # Токены только второго хода, не сумма всей сессии.

asyncio.run(main())
```

Обычный `bridge.ask(...)` по-прежнему создаёт независимую беседу. Сессия
принадлежит одному серверу и одному клиентскому процессу; она изолирована от
других сессий. Если не вызвать `close()`, сервер закроет её после 30 минут
бездействия или при остановке. После перезапуска сервера старую сессию
продолжить нельзя. Antigravity SDK mode не поддерживает сессии; используйте
основной CLI mode. В длинных беседах история может увеличивать расход входных
токенов, поэтому сравнивайте usage, а не предполагайте экономию заранее.

CLI:

```powershell
& .\.venv\Scripts\python.exe -m agent_bridge.cli info http://127.0.0.1:8765
& .\.venv\Scripts\python.exe -m agent_bridge.cli ask http://127.0.0.1:8766 'Проверь проект' `
  --model gemini-3.8-flash-medium --reasoning-effort medium
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

- Каждый одиночный `ask` создаёт новую беседу; `bridge.session(...)` продолжает
  одну беседу между вызовами, но не подключается к открытому диалогу IDE.
- Передаются текстовая задача и ответ. Общие файлы доступны агентам через workspace.
- A2A task store находится в памяти и очищается при перезапуске.
- Серверы не имеют сетевой аутентификации. Не публикуйте порты 8765/8766 без TLS и auth.
- Инструменты агента подчиняются sandbox и permission policy соответствующего harness.
- Usage endpoint раскрывает локальным процессам состояние квот аккаунта.
- `config/read` и `account/rateLimits/read` вызываются через типизированный низкоуровневый транспорт Codex SDK, пока высокоуровневый Python API не предоставляет их напрямую.

См. [SECURITY.md](SECURITY.md) и [CONTRIBUTING.md](CONTRIBUTING.md).

## Документация

- [A2A Protocol](https://a2a-protocol.org/latest/)
- [Antigravity CLI overview](https://antigravity.google/docs/cli/overview/)
- [Antigravity headless mode](https://antigravity.google/docs/cli/headless/)
- [Antigravity MCP](https://antigravity.google/docs/mcp/)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
