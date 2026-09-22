"""Local A2A bridge for configured agent runtimes."""

from .client import BridgeClient, BridgeResult, BridgeSession
from .profiles import AgentProfile, ToolPolicy
from .registry import build_profile

__all__ = [
    "BridgeClient",
    "BridgeResult",
    "BridgeSession",
    "AgentProfile",
    "ToolPolicy",
    "build_profile",
]
