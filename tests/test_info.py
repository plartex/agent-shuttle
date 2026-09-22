import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_bridge.info import AntigravityCliInfo, AntigravitySdkInfo, CodexInfo


class FakeProcess:
    def __init__(self, data, returncode=0):
        self.data = data
        self.returncode = returncode

    async def communicate(self):
        return self.data, b"failed" if self.returncode else b""

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


class AntigravityInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_models_effort_and_quota_are_normalized(self):
        def envelope(data):
            return ("progress\n" + json.dumps({"status": "SUCCESS", "command": {"data": data}}) + "\n").encode()

        async def spawn(*args, **kwargs):
            if "models" in args:
                return FakeProcess(envelope({"models": [{"id": "m"}]}))
            prompt = args[args.index("-p") + 1]
            if prompt == "/model":
                return FakeProcess(envelope({"id": "m", "effort": "low"}))
            if prompt == "/effort":
                return FakeProcess(envelope({"current": "high", "available": ["low", "high"], "adjustable": True}))
            return FakeProcess(envelope({"groups": [{"name": "group", "buckets": [
                {"id": "daily", "remaining_fraction": 0.25, "reset_time": "tomorrow"}
            ]}]}))

        with tempfile.TemporaryDirectory() as folder, \
             patch("agent_bridge.info.asyncio.create_subprocess_exec", side_effect=spawn):
            info = await AntigravityCliInfo(Path(folder)).fetch()
        self.assertEqual(info["capabilities"]["selected_model"], "m")
        self.assertEqual(info["capabilities"]["selected_effort"], "high")
        self.assertEqual(info["usage"]["groups"][0]["buckets"][0]["used_percent"], 75)

    async def test_bad_headless_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            info = AntigravityCliInfo(Path(folder))
            with patch("agent_bridge.info.asyncio.create_subprocess_exec", return_value=FakeProcess(b"", 1)):
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    await info._read("models")
            with patch("agent_bridge.info.asyncio.create_subprocess_exec", return_value=FakeProcess(b"not-json")):
                with self.assertRaisesRegex(RuntimeError, "no JSON"):
                    await info._read("models")
            error = json.dumps({"status": "ERROR", "error": "no account"}).encode()
            with patch("agent_bridge.info.asyncio.create_subprocess_exec", return_value=FakeProcess(error)):
                with self.assertRaisesRegex(RuntimeError, "no account"):
                    await info._read("models")

    async def test_sdk_info_explicitly_marks_unavailable(self):
        result = await AntigravitySdkInfo().fetch()
        self.assertFalse(result["usage"]["available"])
        self.assertFalse(result["capabilities"]["available"])


class CodexInfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_catalog_and_rate_limits(self):
        model = SimpleNamespace(
            model="test-model", id="test-id", display_name="Test", is_default=True,
            default_reasoning_effort="medium",
            supported_reasoning_efforts=[SimpleNamespace(reasoning_effort="high")],
        )
        class Client:
            async def request(self, method, *args, **kwargs):
                if method == "config/read":
                    return SimpleNamespace(config=SimpleNamespace(model="test-model", model_reasoning_effort="high"))
                return SimpleNamespace(
                    ordinary_usage_allowed=True, rate_limit_reset_credits=None,
                    rate_limits_by_limit_id={"core": {
                        "limitName": "Core", "normalModelSlug": "test-model", "planType": "pro",
                        "primary": {"usedPercent": 20, "resetsAt": 1760000000, "windowDurationMins": 300},
                        "secondary": None,
                    }},
                )

        class Codex:
            def __init__(self, config):
                self._client = Client()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def models(self):
                return SimpleNamespace(data=[model])

        with tempfile.TemporaryDirectory() as folder, \
             patch("openai_codex.AsyncCodex", Codex):
            result = await CodexInfo(Path(folder)).fetch()
        self.assertEqual(result["capabilities"]["selected_effort"], "high")
        self.assertEqual(result["capabilities"]["models"][0]["efforts"], ["high"])
        self.assertEqual(result["usage"]["groups"][0]["buckets"][0]["remaining_percent"], 80)


if __name__ == "__main__":
    unittest.main()
