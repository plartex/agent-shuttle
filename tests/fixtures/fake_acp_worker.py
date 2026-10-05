"""Small ACP wire peer: intentionally independent of the Python ACP SDK."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def options(model, effort):
    return [{"type": "select", "id": key, "name": key, "currentValue": current,
             "options": [{"value": value, "name": value} for value in values]}
            for key, current, values in (("model", model, ("small", "large")),
                                         ("effort", effort, ("low", "high")))]


model = "small"
effort = "low"
pending = None
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")
    if method == "initialize":
        if "hang_init" in sys.argv:
            time.sleep(60)
        if "spawn_child" in sys.argv:
            child = subprocess.Popen([
                sys.executable, "-c",
                "from pathlib import Path; import time; p=Path('acp-heartbeat.txt'); "
                "exec('while True:\\n p.write_text(str(time.time()))\\n time.sleep(0.1)')",
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL)
            Path("acp-child.pid").write_text(str(child.pid), encoding="utf-8")
        result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": "no_load" not in sys.argv},
                  "authMethods": []}
    elif method == "session/new":
        result = {"sessionId": str(uuid.uuid4()), "configOptions": options(model, effort)}
    elif method == "session/load":
        result = {"configOptions": options(model, effort)}
    elif method == "session/set_config_option":
        if params["configId"] == "model":
            model = params["value"]
        else:
            effort = params["value"]
        result = {"configOptions": options(model, effort)}
    elif method == "session/prompt":
        message = params["prompt"][0]["text"]
        if message == "crash":
            os._exit(7)
        if message == "wait":
            Path("acp-waiting.flag").write_text("waiting", encoding="utf-8")
            pending = request_id
            continue
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": params["sessionId"],
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"{message}:{model}"}},
        }})
        result = {"stopReason": "end_turn"}
    elif method == "session/cancel":
        Path("acp-cancelled.flag").write_text("cancelled", encoding="utf-8")
        if pending is not None:
            send({"jsonrpc": "2.0", "id": pending, "result": {"stopReason": "cancelled"}})
            pending = None
        continue
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": method}})
        continue
    send({"jsonrpc": "2.0", "id": request_id, "result": result})
