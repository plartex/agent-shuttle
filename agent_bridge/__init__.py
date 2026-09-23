"""Local A2A bridge for configured agent runtimes."""

from .client import BridgeClient, BridgeResult, BridgeSession
from .discovery import discover_harnesses
from .profiles import AgentProfile, ToolPolicy
from .registry import build_profile

__all__ = [
    "BridgeClient",
    "BridgeResult",
    "BridgeSession",
    "discover_harnesses",
    "AgentProfile",
    "ToolPolicy",
    "build_profile",
]
