# Изолированные правки задач

`workspace_mode="isolated"` — дополнительный режим **отдельной задачи**. Он создаёт detached Git worktree от текущего `HEAD`. Агент работает в этой копии; завершение задачи и чтение diff не изменяют исходный каталог. По умолчанию остаётся `shared`.

Для запуска нужны Git-репозиторий с коммитом, чистая исходная рабочая копия, `tool_policy="workspace_write"` и backend, который действительно ограничивает запись рабочим каталогом. Сессии и backend с рекомендательной политикой инструментов в этом режиме отклоняются. Worktree отделяет правки задач, но не является песочницей.

```python
async with TaskManager.for_workspace(project_dir) as manager:
    task = await manager.dispatch(
        "codex", "Исправь ошибку", tool_policy="workspace_write",
        workspace_mode="isolated",
    )
    await task.wait()
    changes = await task.changes()
    info = await changes.info()
    page = await changes.diff(cursor=0, limit=60000)
    # Изучите все страницы до next_cursor == None.
    outcome = await changes.apply(expected_revision=info["revision"])
```

`ChangeSet.info()` возвращает ID задачи, состояние правок, revision (Git commit), список путей и предупреждения. `diff()` выдаёт постраничный Git binary patch. `apply()` требует `expected_revision`; повторное применение возвращает `already_applied`. Для частичного артефакта неудачной или отменённой задачи нужно явно передать `allow_partial=True`. `discard()` удаляет сохранённый артефакт и рабочую копию. Свой `WorkspaceProvider` и `backend_factories={agent_id: factory_for_path}` можно передать в `TaskManager`; фабрика обязана создавать backend для полученного пути.

MCP: `submit_task(..., workspace_mode="isolated", tool_policy="workspace_write")`, затем `get_task_changes`, постраничный `get_task_diff`, `apply_task_changes(task_id, expected_revision, allow_partial=False)` или `discard_task_changes`. Diff также доступен как resource `agent-shuttle://tasks/{task_id}/diff` для небольших изменений. MCP-инструменты работают с тем же артефактом через авторизованный A2A-пир; A2A-результат содержит краткое описание в `agent_shuttle.change`.

Состояния правок: `running`, `ready`, `partial`, `applying`, `applied`, `conflict`, `inspection_required`, `discarded`. Перед применением Shuttle сверяет затронутые пути с исходным commit и запускает `git apply --check` под блокировкой. При конфликте исходные файлы не меняются. После сбоя во время применения неоднозначный результат получает `inspection_required` и автоматически не записывается повторно. Артефакт сохраняется под `refs/agent-shuttle/changes/<task_id>`, метаданные — в SQLite, рабочая копия остаётся до явного `discard`; при ошибке сохранения она также остаётся для восстановления. Игнорируемые файлы и незакоммиченные изменения внутри submodules не входят в артефакт.
