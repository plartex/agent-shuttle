"""Durable, reviewable workspace changes independent of the agent transport."""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol


class WorkspaceProvider(Protocol):
    """A library caller may supply another workspace and artifact implementation."""

    def prepare(self, task_id: str) -> dict: ...
    def capture(self, change: dict, *, partial: bool) -> dict: ...
    def diff(self, change: dict) -> str: ...
    def apply(self, change: dict) -> str: ...
    def reconcile(self, change: dict) -> str: ...
    def discard(self, change: dict) -> None: ...


def _git(root: Path, *args: str, env: dict | None = None, input: bytes | None = None,
         check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    process = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", *args],
        input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})}, timeout=timeout, check=False,
    )
    if check and process.returncode:
        message = process.stderr.decode("utf-8", "replace").strip()[-1500:]
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {message}")
    return process


@contextmanager
def _apply_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GitWorktreeProvider:
    """Use a detached worktree and hidden Git ref for each task's final tree."""

    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve(strict=True)
        self.state_root = self.workspace / ".agent-shuttle"
        self.worktrees = self.state_root / "worktrees"

    def _owned_path(self, task_id: str) -> Path:
        task_id = str(uuid.UUID(task_id))
        path = self.worktrees / task_id
        if path.parent != self.worktrees:
            raise ValueError("Worktree path escaped the managed root")
        return path

    def prepare(self, task_id: str) -> dict:
        root = Path(_git(self.workspace, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
        if root != self.workspace:
            raise ValueError("Isolated mode requires the Git repository root as workspace")
        base = _git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode().strip()
        dirty = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                     "--", ".", ":(exclude).agent-shuttle/").stdout
        if dirty:
            raise ValueError("Isolated mode requires a clean source checkout")
        path = self._owned_path(task_id)
        if path.exists() or path.is_symlink():
            raise ValueError("Managed worktree path already exists")
        self.worktrees.mkdir(parents=True, exist_ok=True)
        _git(root, "worktree", "add", "--detach", str(path), base)
        return {"task_id": task_id, "workspace_path": str(path), "base_oid": base,
                "result_oid": None, "revision": None, "state": "running", "files": []}

    def _temp_index(self):
        folder = self.state_root / "indexes"
        folder.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix="index-", dir=folder)
        os.close(descriptor)
        Path(filename).unlink()
        return Path(filename)

    def _tree_from_workspace(self, root: Path, base: str, paths: list[str] | None = None) -> str:
        index = self._temp_index()
        env = {"GIT_INDEX_FILE": str(index)}
        try:
            _git(root, "read-tree", base, env=env)
            _git(root, "add", "-A", "--", ".", ":(exclude).agent-shuttle/", env=env)
            if paths:
                for path in paths:
                    if (root / path).exists():
                        _git(root, "add", "-f", "-A", "--", path, env=env)
            return _git(root, "write-tree", env=env).stdout.decode().strip()
        finally:
            index.unlink(missing_ok=True)

    def capture(self, change: dict, *, partial: bool) -> dict:
        path = Path(change["workspace_path"])
        base = change["base_oid"]
        tree = self._tree_from_workspace(path, base)
        identity = {"GIT_AUTHOR_NAME": "Agent Shuttle", "GIT_AUTHOR_EMAIL": "agent-shuttle@localhost",
                    "GIT_COMMITTER_NAME": "Agent Shuttle", "GIT_COMMITTER_EMAIL": "agent-shuttle@localhost"}
        result = _git(self.workspace, "commit-tree", tree, "-p", base, "-m",
                      f"Agent Shuttle task {change['task_id']}", env=identity).stdout.decode().strip()
        ref = f"refs/agent-shuttle/changes/{change['task_id']}"
        _git(self.workspace, "update-ref", ref, result)
        _git(self.workspace, "cat-file", "-e", f"{result}^{{commit}}")
        raw_paths = _git(self.workspace, "diff", "--name-only", "-z", base, result).stdout
        files = [item.decode("utf-8", "replace") for item in raw_paths.split(b"\0") if item]
        return {**change, "result_oid": result, "revision": result,
                "state": "partial" if partial else "ready", "partial": partial, "files": files}

    def diff(self, change: dict) -> str:
        if not change.get("result_oid"):
            return ""
        return _git(self.workspace, "diff", "--binary", "--no-ext-diff",
                    change["base_oid"], change["result_oid"], "--").stdout.decode("utf-8", "replace")

    def _matches(self, change: dict, target_oid: str) -> bool:
        files = change["files"]
        if not files:
            return True
        tree = self._tree_from_workspace(self.workspace, change["base_oid"], files)
        index = self._temp_index()
        env = {"GIT_INDEX_FILE": str(index)}
        try:
            _git(self.workspace, "read-tree", tree, env=env)
            result = _git(self.workspace, "diff", "--cached", "--quiet", target_oid,
                          "--", *files, env=env, check=False)
            if result.returncode not in {0, 1}:
                raise RuntimeError("Could not compare target tree")
            return result.returncode == 0
        finally:
            index.unlink(missing_ok=True)

    def _source_index_matches_base(self, change: dict) -> bool:
        if not change["files"]:
            return True
        result = _git(self.workspace, "diff", "--cached", "--quiet", change["base_oid"],
                      "--", *change["files"], check=False)
        if result.returncode not in {0, 1}:
            raise RuntimeError("Could not compare the source Git index")
        return result.returncode == 0

    def apply(self, change: dict) -> str:
        with _apply_lock(self.state_root / "apply.lock"):
            if (not self._matches(change, change["base_oid"])
                    or not self._source_index_matches_base(change)):
                return "conflict"
            patch = _git(self.workspace, "diff", "--binary", "--no-ext-diff",
                         change["base_oid"], change["result_oid"], "--").stdout
            probe = _git(self.workspace, "apply", "--check", "--binary", "-",
                         input=patch, check=False)
            if probe.returncode:
                return "conflict"
            applied = _git(self.workspace, "apply", "--binary", "-", input=patch, check=False)
            if applied.returncode:
                return "inspection_required"
            return "applied" if self._matches(change, change["result_oid"]) else "inspection_required"

    def reconcile(self, change: dict) -> str:
        with _apply_lock(self.state_root / "apply.lock"):
            if self._matches(change, change["result_oid"]):
                return "applied"
            if self._matches(change, change["base_oid"]):
                return "partial" if change.get("partial") else "ready"
            return "inspection_required"

    def discard(self, change: dict) -> None:
        path = self._owned_path(change["task_id"])
        if path.exists():
            _git(self.workspace, "worktree", "remove", "--force", str(path))
        if change.get("result_oid"):
            _git(self.workspace, "update-ref", "-d",
                 f"refs/agent-shuttle/changes/{change['task_id']}", change["result_oid"])
