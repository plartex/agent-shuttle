# Upgrade to Agent Shuttle 0.7

Version 0.7 changes names across the Python API and local protocol. Install Agent Shuttle 0.7 and Qriterra 0.7 together. Finish active tasks, stop old local servers, update launch configuration, and restart the servers. The old names are not accepted by the new release.

| Before 0.7 | From 0.7 |
|---|---|
| `BridgeClient`, `BridgeSession`, `BridgeResult`, `BridgeEvent`, `BridgeConnection` | `ShuttleClient`, `ShuttleSession`, `ShuttleResult`, `ShuttleEvent`, `ShuttleConnection` |
| `BRIDGE_WORKSPACE`, `BRIDGE_TASK_REGISTRY`, `BRIDGE_DEBUG`, `BRIDGE_AGENTS_JSON` | `AGENT_SHUTTLE_WORKSPACE`, `AGENT_SHUTTLE_TASK_REGISTRY`, `AGENT_SHUTTLE_DEBUG`, `AGENT_SHUTTLE_AGENTS_JSON` |
| `BRIDGE_<HARNESS>_URL`, `BRIDGE_<HARNESS>_WORKSPACE`, `BRIDGE_AGY_COMMAND` | `AGENT_SHUTTLE_<HARNESS>_URL`, `AGENT_SHUTTLE_<HARNESS>_WORKSPACE`, `AGENT_SHUTTLE_AGY_COMMAND` |
| `BRIDGE_LIVE_*` test settings | `AGENT_SHUTTLE_LIVE_*` |
| `/bridge/*` | `/shuttle/*` |
| A2A metadata `agent_bridge.<field>` | `agent_shuttle.<field>` |
| Internal worker marker `AGENT_BRIDGE_RESULT` | `AGENT_SHUTTLE_RESULT` |
| Qriterra `AgentBridgeProvider`, `agent_bridge_provider_from_name`, `agent-bridge:<agent>` | `AgentShuttleProvider`, `agent_shuttle_provider_from_name`, `agent-shuttle:<agent>` |

The suffixes of A2A metadata keys and the behavior of each endpoint remain the same. Agent Shuttle migrates stored A2A tasks, request fingerprints, and library event metadata when opening an older SQLite database. It creates a `*.pre-0.7-<id>.sqlite3` backup beside each database before migration. A failed migration aborts startup and leaves the original database available. Existing Qriterra JSON reports remain readable as historical files; new reports use `provider.id = "agent_shuttle"`.

Old servers still expose the old routes. Restart them after upgrading both packages; mixing 0.6 and 0.7 clients or servers is unsupported.
