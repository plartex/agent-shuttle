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

- Каждый `ask` создаёт новую сессию агента; открытый диалог IDE не продолжается.
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
