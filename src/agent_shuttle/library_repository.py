"""SQLite persistence for the transport-independent task library."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from time import time

from .metadata_migration import backup_database, rename_json_metadata


class LibraryTaskRepository:
    """Own the library database, its schema, lock and all SQL transactions."""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path is not None else None
        self._conn: sqlite3.Connection | None = None
        self._owner = None

    def open(self) -> None:
        if self._conn is not None:
            raise RuntimeError("LibraryTaskRepository is already open")
        existed = self.path is not None and self.path.exists()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        owner = (self.path.with_suffix(self.path.suffix + ".owner").open("a+b")
                 if self.path is not None else None)
        conn = None
        try:
            if owner is not None:
                if owner.seek(0, 2) == 0:
                    owner.write(b"0")
                    owner.flush()
                owner.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            conn = sqlite3.connect(self.path or ":memory:", timeout=15)
            conn.row_factory = sqlite3.Row
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError(f"Unsupported library database schema version {version}")
            if version == 0 and existed:
                backup_database(self.path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, session_id TEXT,
                    request_id TEXT UNIQUE, fingerprint TEXT, prompt TEXT NOT NULL,
                    model TEXT, reasoning_effort TEXT, tool_policy TEXT,
                    state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    last_activity_at REAL, text TEXT NOT NULL DEFAULT '',
                    usage TEXT, details TEXT, error TEXT,
                    observed_model TEXT, warnings TEXT, files_changed TEXT,
                    files_changed_state TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE INDEX IF NOT EXISTS idx_library_tasks_session ON tasks(session_id);
                CREATE TABLE IF NOT EXISTS events (
                    task_id TEXT NOT NULL, seq INTEGER NOT NULL, timestamp REAL NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(task_id, seq)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, model TEXT,
                    reasoning_effort TEXT, tool_policy TEXT, state TEXT NOT NULL,
                    native_id TEXT, resume_supported INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    agent_id TEXT PRIMARY KEY, model TEXT, reasoning_effort TEXT
                );
                CREATE TABLE IF NOT EXISTS task_changes (
                    task_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    workspace_path TEXT NOT NULL, base_oid TEXT NOT NULL,
                    result_oid TEXT, revision TEXT, files TEXT NOT NULL DEFAULT '[]',
                    error TEXT, partial INTEGER NOT NULL DEFAULT 0
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "native_id" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN native_id TEXT")
            if "resume_supported" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN resume_supported INTEGER NOT NULL DEFAULT 0")
                conn.execute("UPDATE sessions SET resume_supported=1 WHERE native_id IS NOT NULL")
            task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            change_columns = {row["name"] for row in conn.execute("PRAGMA table_info(task_changes)")}
            if "partial" not in change_columns:
                conn.execute("ALTER TABLE task_changes ADD COLUMN partial INTEGER NOT NULL DEFAULT 0")
            if "warnings" not in task_columns:
                conn.execute("ALTER TABLE tasks ADD COLUMN warnings TEXT")
            for table in ("tasks", "sessions"):
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "output_schema" not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN output_schema TEXT")
            conn.commit()
            if version == 0:
                conn.execute("BEGIN IMMEDIATE")
                for task_id, seq, raw in conn.execute(
                    "SELECT task_id, seq, data FROM events"
                ).fetchall():
                    if "agent_bridge." in raw:
                        conn.execute(
                            "UPDATE events SET data=? WHERE task_id=? AND seq=?",
                            (rename_json_metadata(raw), task_id, seq),
                        )
                for column in ("usage", "details", "error", "warnings"):
                    for task_id, raw in conn.execute(
                        f"SELECT id, {column} FROM tasks WHERE {column} IS NOT NULL"
                    ).fetchall():
                        if "agent_bridge." in raw:
                            conn.execute(
                                f"UPDATE tasks SET {column}=? WHERE id=?",
                                (rename_json_metadata(raw), task_id),
                            )
                conn.execute("PRAGMA user_version=1")
            conn.commit()
            self._owner, self._conn = owner, conn
        except BaseException:
            if conn is not None:
                conn.close()
            if owner is not None:
                owner.close()
            raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._owner is not None:
            self._owner.close()
            self._owner = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("TaskManager must be used inside 'async with'")
        return self._conn

    def ensure_open(self) -> None:
        self._db()

    @staticmethod
    def _record(row):
        return dict(row) if row is not None else None

    @staticmethod
    def _append(conn, task_id: str, kind: str, data: dict):
        seq = conn.execute("SELECT COALESCE(MAX(seq), -1)+1 FROM events WHERE task_id=?", (task_id,)).fetchone()[0]
        payload = json.dumps(data, ensure_ascii=False, default=str)
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?)", (task_id, seq, time(), kind, payload))

    def find_task(self, task_id: str):
        return self._record(self._db().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def task(self, task_id: str):
        row = self.find_task(task_id)
        if row is None:
            raise KeyError(f"Unknown task {task_id}")
        return row

    def task_by_request(self, request_id: str):
        return self._record(self._db().execute(
            "SELECT id, fingerprint FROM tasks WHERE request_id=?", (request_id,)).fetchone())

    def change(self, task_id: str):
        row = self._record(self._db().execute(
            "SELECT * FROM task_changes WHERE task_id=?", (task_id,)).fetchone())
        if row is not None:
            row["files"] = json.loads(row["files"])
            row["partial"] = bool(row["partial"])
        return row

    def list_changes(self, state: str | None = None):
        query = "SELECT task_id FROM task_changes" + (" WHERE state=?" if state else "")
        return [self.change(row[0]) for row in self._db().execute(query, (state,) if state else ())]

    def save_change(self, change: dict) -> None:
        self._db().execute("""INSERT INTO task_changes
            (task_id,state,workspace_path,base_oid,result_oid,revision,files,error,partial)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
            state=excluded.state, workspace_path=excluded.workspace_path,
            base_oid=excluded.base_oid, result_oid=excluded.result_oid,
            revision=excluded.revision, files=excluded.files, error=excluded.error,
            partial=excluded.partial""",
            (change["task_id"], change["state"], change["workspace_path"],
             change["base_oid"], change.get("result_oid"), change.get("revision"),
             json.dumps(change.get("files", [])), change.get("error"), int(change.get("partial", False))))
        self._db().commit()

    def set_change_state(self, task_id: str, state: str, error: str | None = None) -> None:
        self._db().execute("UPDATE task_changes SET state=?, error=? WHERE task_id=?",
                           (state, error, task_id))
        self._db().commit()

    def list_tasks(self, session_id: str | None = None):
        if session_id is None:
            rows = self._db().execute("SELECT * FROM tasks ORDER BY created_at, id")
        else:
            rows = self._db().execute(
                "SELECT * FROM tasks WHERE session_id=? ORDER BY created_at, id", (session_id,))
        return [dict(row) for row in rows.fetchall()]

    def create_task(self, values: dict, submitted_event: dict) -> None:
        conn = self._db()
        now = time()
        try:
            conn.execute("""INSERT INTO tasks
                (id, agent_id, session_id, request_id, fingerprint, prompt, model,
                 reasoning_effort, tool_policy, state, created_at, updated_at,
                 last_activity_at, output_schema)
                VALUES (?,?,?,?,?,?,?,?,?,'submitted',?,?,?,?)""",
                (values["id"], values["agent_id"], values.get("session_id"),
                 values.get("request_id"), values.get("fingerprint"), values["prompt"],
                 values.get("model"), values.get("reasoning_effort"), values.get("tool_policy"),
                 now, now, now, values.get("output_schema")))
            self._append(conn, values["id"], "submitted", submitted_event)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def update_task(self, task_id: str, state: str, data: dict | None = None, **fields) -> bool:
        conn = self._db()
        old = self.task(task_id)
        if old["state"] in {"completed", "failed", "canceled", "rejected"}:
            return False
        if state in {"completed", "failed", "canceled", "rejected"} and fields.get("files_changed_state") is None:
            fields["files_changed_state"] = "unavailable"
        now = time()
        try:
            conn.execute("""UPDATE tasks SET state=?, updated_at=?, last_activity_at=?,
                text=COALESCE(?,text), usage=COALESCE(?,usage), details=COALESCE(?,details),
                error=COALESCE(?,error), observed_model=COALESCE(?,observed_model),
                warnings=COALESCE(?,warnings), files_changed=COALESCE(?,files_changed),
                files_changed_state=COALESCE(?,files_changed_state) WHERE id=?""",
                (state, now, now, fields.get("text"),
                 json.dumps(fields["usage"]) if fields.get("usage") is not None else None,
                 json.dumps(fields["details"]) if fields.get("details") is not None else None,
                 json.dumps(fields["error"]) if fields.get("error") is not None else None,
                 fields.get("observed_model"),
                 json.dumps(fields["warnings"]) if fields.get("warnings") is not None else None,
                 json.dumps(fields["files_changed"]) if fields.get("files_changed") is not None else None,
                 fields.get("files_changed_state"), task_id))
            self._append(conn, task_id, state if data is None else data.get("kind", state), data or {})
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return True

    def events(self, task_id: str, cursor: int, limit: int):
        return [dict(row) for row in self._db().execute(
            "SELECT seq, timestamp, kind, data FROM events WHERE task_id=? AND seq>=? ORDER BY seq LIMIT ?",
            (task_id, cursor, limit)).fetchall()]

    def event(self, task_id: str, seq: int):
        row = self._db().execute("SELECT data FROM events WHERE task_id=? AND seq=?",
                                 (task_id, seq)).fetchone()
        return None if row is None else row["data"]

    def event_count(self, task_id: str) -> int:
        return self._db().execute("SELECT COUNT(*) FROM events WHERE task_id=?", (task_id,)).fetchone()[0]

    def preference(self, agent_id: str):
        row = self._db().execute("SELECT model, reasoning_effort FROM preferences WHERE agent_id=?",
                                 (agent_id,)).fetchone()
        return {"model": row["model"] if row else None,
                "reasoning_effort": row["reasoning_effort"] if row else None}

    def set_preference(self, agent_id: str, model: str | None, reasoning_effort: str | None):
        conn = self._db()
        conn.execute("INSERT OR REPLACE INTO preferences VALUES (?,?,?)", (agent_id, model, reasoning_effort))
        conn.commit()
        return self.preference(agent_id)

    def find_session(self, session_id: str):
        return self._record(self._db().execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def session(self, session_id: str):
        row = self.find_session(session_id)
        if row is None:
            raise KeyError(f"Unknown session {session_id}")
        return row

    def create_session(self, session_id: str, agent_id: str, model: str | None,
                       reasoning_effort: str | None, tool_policy: str | None,
                       output_schema: str | None):
        conn = self._db()
        now = time()
        conn.execute("""INSERT INTO sessions
            (id,agent_id,model,reasoning_effort,tool_policy,state,created_at,updated_at,output_schema)
            VALUES (?,?,?,?,?,'open',?,?,?)""",
            (session_id, agent_id, model, reasoning_effort, tool_policy, now, now, output_schema))
        conn.commit()

    def list_sessions(self):
        return [dict(row) for row in self._db().execute(
            "SELECT * FROM sessions ORDER BY created_at, id").fetchall()]

    def idle_sessions(self, cutoff: float):
        return [row["id"] for row in self._db().execute(
            "SELECT id FROM sessions WHERE state IN ('open','suspended') AND updated_at<? ORDER BY updated_at",
            (cutoff,)).fetchall()]

    def set_session_state(self, session_id: str, state: str, native_id: str | None = None,
                          resume_supported: bool = False):
        conn = self._db()
        if state == "open":
            conn.execute("UPDATE sessions SET state='open', native_id=?, resume_supported=?, updated_at=? WHERE id=?",
                         (native_id, int(resume_supported), time(), session_id))
        elif state == "interrupted":
            conn.execute("UPDATE sessions SET state='interrupted', updated_at=? WHERE id=? AND state IN ('open','suspended')",
                         (time(), session_id))
        else:
            conn.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?",
                         (state, time(), session_id))
        conn.commit()

    def touch_session(self, session_id: str):
        conn = self._db()
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=? AND state='open'", (time(), session_id))
        conn.commit()

    def recover_interrupted(self, resumable_agents: set[str]):
        conn = self._db()
        now = time()
        try:
            interrupted = conn.execute(
                "SELECT id, session_id FROM tasks WHERE state IN ('submitted','working')").fetchall()
            interrupted_sessions = {row["session_id"] for row in interrupted if row["session_id"]}
            for row in interrupted:
                error = {"code": "worker_interrupted", "type": "WorkerInterrupted",
                         "message": "Worker stopped before task completion; execution was not replayed",
                         "retryable": False}
                conn.execute("UPDATE tasks SET state='failed', updated_at=?, error=?, files_changed_state='unavailable' WHERE id=?",
                             (now, json.dumps(error), row["id"]))
                self._append(conn, row["id"], "failed", error)
            for row in conn.execute(
                    "SELECT id, agent_id, native_id, resume_supported FROM sessions WHERE state IN ('open','suspended')").fetchall():
                resumable = (row["native_id"] is not None and row["id"] not in interrupted_sessions
                             and row["agent_id"] in resumable_agents and bool(row["resume_supported"]))
                conn.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?",
                             ("suspended" if resumable else "interrupted", now, row["id"]))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def suspend_open_sessions(self, resumable_agents: set[str]):
        conn = self._db()
        now = time()
        for row in conn.execute("SELECT id, agent_id, native_id, resume_supported FROM sessions WHERE state='open'").fetchall():
            resumable = (row["native_id"] is not None and row["agent_id"] in resumable_agents
                         and bool(row["resume_supported"]))
            conn.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?",
                         ("suspended" if resumable else "interrupted", now, row["id"]))
        conn.commit()
