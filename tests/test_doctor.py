import json
import os
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_shuttle.cli import main
from agent_shuttle.doctor import DoctorConfigError, diagnose, format_report


class DoctorTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_checks_installed_agents_without_a_model_turn(self):
        backend = SimpleNamespace(run=AsyncMock())
        info = SimpleNamespace(fetch=AsyncMock(return_value={}))
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "{}"}), \
             patch("agent_shuttle.doctor.discover_harnesses", return_value={"codex": "agent-shuttle"}), \
             patch("agent_shuttle.doctor.build_builtin", return_value=(backend, info)):
            report = await diagnose()
        self.assertEqual(report["status"], "OK")
        self.assertEqual([check["status"] for check in report["checks"]],
                         ["OK", "SKIP", "SKIP", "SKIP"])
        self.assertFalse(any(check["turn_verified"] for check in report["checks"]))
        backend.run.assert_not_awaited()
        info.fetch.assert_awaited_once_with(capabilities=True, usage=False)

    async def test_antigravity_readiness_failure_is_not_reported_as_install_failure(self):
        info = SimpleNamespace(check_ready=AsyncMock(side_effect=RuntimeError("secret token")))
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "{}"}), \
             patch("agent_shuttle.doctor.discover_harnesses", return_value={"antigravity": "agy"}), \
             patch("agent_shuttle.doctor.build_builtin", return_value=(object(), info)):
            report = await diagnose("antigravity")
        self.assertEqual(report["checks"][0]["level"], "connect")
        self.assertEqual(report["checks"][0]["status"], "FAIL")
        self.assertNotIn("secret token", json.dumps(report))

    async def test_smoke_is_opt_in_and_uses_enforced_policy(self):
        for target, policy in (("codex", "read_only"), ("antigravity", "no_tools")):
            with self.subTest(target=target):
                backend = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text="OK")))
                info = (SimpleNamespace(fetch=AsyncMock(return_value={})) if target == "codex"
                        else SimpleNamespace(check_ready=AsyncMock()))
                with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "{}"}), \
                     patch("agent_shuttle.doctor.discover_harnesses", return_value={target: "command"}), \
                     patch("agent_shuttle.doctor.build_builtin", return_value=(backend, info)):
                    report = await diagnose(target, smoke=True)
                self.assertEqual(report["status"], "OK")
                self.assertTrue(report["checks"][0]["turn_verified"])
                self.assertEqual(report["checks"][0]["output"], "OK")
                self.assertIn('Output: "OK"', format_report(report))
                self.assertEqual(backend.run.call_args.kwargs["tool_policy"], policy)

    async def test_smoke_failure_and_missing_command(self):
        backend = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("provider error")))
        info = SimpleNamespace(fetch=AsyncMock(return_value={}))
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "{}"}), \
             patch("agent_shuttle.doctor.discover_harnesses", return_value={"codex": "agent-shuttle"}), \
             patch("agent_shuttle.doctor.build_builtin", return_value=(backend, info)):
            report = await diagnose("codex", smoke=True)
        self.assertEqual(report["checks"][0]["status"], "FAIL")
        self.assertFalse(report["checks"][0]["turn_verified"])
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "{}"}), \
             patch("agent_shuttle.doctor.discover_harnesses", return_value={}):
            missing = await diagnose("codex")
        self.assertEqual(missing["checks"][0]["level"], "install")
        self.assertEqual(missing["checks"][0]["status"], "FAIL")

    async def test_profile_probe_and_smoke_close_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({
                "id": "local", "runtime": "claude_code", "provider": "ollama",
                "workspace": folder, "default_model": "small", "allowed_models": ["small"],
            }), encoding="utf-8")
            backend = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text="OK")),
                                      close=AsyncMock())
            info = SimpleNamespace(fetch=AsyncMock(return_value={}))
            with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "{}"}), \
                 patch("agent_shuttle.doctor.build_profile", return_value=(backend, info)):
                probe = await diagnose(profile_path=path)
                smoke = await diagnose(profile_path=path, smoke=True)
        self.assertEqual(probe["checks"][0]["level"], "connect")
        self.assertEqual(smoke["checks"][0]["level"], "turn")
        backend.run.assert_awaited_once()
        self.assertEqual(backend.run.call_args.kwargs["tool_policy"], "no_tools")
        self.assertEqual(backend.close.await_count, 2)

    async def test_registered_profile_and_probe_timeout_close_runtime(self):
        async def slow_fetch(**_):
            await asyncio.sleep(1)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({
                "id": "profile-name", "runtime": "claude_code", "provider": "ollama",
                "workspace": folder, "default_model": "small", "allowed_models": ["small"],
            }), encoding="utf-8")
            backend = SimpleNamespace(close=AsyncMock())
            info = SimpleNamespace(fetch=AsyncMock(side_effect=slow_fetch))
            mapping = json.dumps({"registered": {"profile": str(path)}})
            with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": mapping}), \
                 patch("agent_shuttle.doctor.discover_harnesses", return_value={}), \
                 patch("agent_shuttle.doctor.build_profile", return_value=(backend, info)), \
                 patch("agent_shuttle.doctor.PROBE_TIMEOUT", 0.01):
                report = await diagnose("registered")
        self.assertEqual(report["checks"][0]["target"], "registered")
        self.assertEqual(report["checks"][0]["status"], "FAIL")
        self.assertIn("timed out", report["checks"][0]["message"])
        backend.close.assert_awaited_once()

    async def test_acp_handshake_does_not_prompt_and_smoke_is_rejected(self):
        fixture = Path(__file__).parent / "fixtures" / "fake_acp_worker.py"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text(json.dumps({"id": "acp-test", "runtime": "acp",
                                        "workspace": folder,
                                        "command": [sys.executable, str(fixture)]}), encoding="utf-8")
            with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "{}"}):
                probe = await diagnose(profile_path=path)
                smoke = await diagnose(profile_path=path, smoke=True)
        self.assertEqual(probe["checks"][0]["status"], "OK")
        self.assertEqual(smoke["checks"][0]["status"], "FAIL")
        self.assertEqual(smoke["checks"][0]["level"], "turn")
        self.assertFalse(smoke["checks"][0]["turn_verified"])

    async def test_configuration_errors(self):
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "{}"}):
            with self.assertRaises(DoctorConfigError):
                await diagnose(smoke=True)
        with patch.dict(os.environ, {"BRIDGE_AGENTS_JSON": "not json"}):
            with self.assertRaises(DoctorConfigError):
                await diagnose()


class DoctorCliTests(unittest.TestCase):
    def test_json_and_exit_codes(self):
        with patch("sys.argv", ["agent-shuttle", "doctor", "codex", "--json"]), \
             patch("agent_shuttle.doctor.diagnose", new_callable=AsyncMock,
                   return_value={"schema_version": 1, "status": "FAIL", "checks": []}), \
             patch("builtins.print") as output:
            with self.assertRaises(SystemExit) as exit_result:
                main()
        self.assertEqual(exit_result.exception.code, 1)
        self.assertEqual(json.loads(output.call_args.args[0])["status"], "FAIL")
        with patch("sys.argv", ["agent-shuttle", "doctor", "--smoke", "--json"]), \
             patch("builtins.print") as output:
            with self.assertRaises(SystemExit) as exit_result:
                main()
        self.assertEqual(exit_result.exception.code, 2)
        self.assertEqual(json.loads(output.call_args.args[0])["status"], "ERROR")
