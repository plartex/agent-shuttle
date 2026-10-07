"""Local A2A shuttle for configured agent runtimes."""

from .backends import AntigravityAuthenticationError, AntigravityPermissionDenied
from .client import ShuttleClient, ShuttleEvent, ShuttleResult, ShuttleSession, TaskHandle
from .discovery import discover_harnesses
from .managed import ShuttleConnection, HarnessLaunch, connect_harness
from .profiles import AgentProfile, ToolPolicy
from .registry import build_profile
from .task_store import SQLiteTaskStore
from .task_library import TaskManager, Task, TaskStatus, TaskResult, ChangeSet, Session, SessionInfo, AgentInfo
from .workspace_changes import WorkspaceProvider, GitWorktreeProvider

__all__ = [
    "ShuttleClient",
    "AntigravityPermissionDenied",
    "AntigravityAuthenticationError",
    "ShuttleResult",
    "ShuttleEvent",
    "ShuttleSession",
    "TaskHandle",
    "discover_harnesses",
    "ShuttleConnection",
    "HarnessLaunch",
    "connect_harness",
    "AgentProfile",
    "ToolPolicy",
    "build_profile",
    "SQLiteTaskStore",
    "TaskManager",
    "Task",
    "TaskStatus",
    "TaskResult",
    "ChangeSet",
    "WorkspaceProvider",
    "GitWorktreeProvider",
    "Session",
    "SessionInfo",
    "AgentInfo",
]
