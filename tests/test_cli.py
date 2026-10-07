import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_shuttle.cli import main


class CliTests(unittest.TestCase):
    def test_discover_acp_profile_checks_command_without_starting_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "acp.json"
            profile.write_text(json.dumps({
                "id": "configured", "runtime": "acp", "workspace": folder,
                "command": [sys.executable, "-c", "raise AssertionError('started')"],
            }), encoding="utf-8")
            with patch("sys.argv", ["agent-shuttle", "discover", "--profile", str(profile)]), \
                 patch("agent_shuttle.cli.discover_harnesses", return_value={}), \
                 patch("builtins.print") as output:
                main()
            discovered = json.loads(output.call_args.args[0])["configured"]
            self.assertTrue(discovered["command_found"])
            self.assertEqual(discovered["protocol"], "acp")

    def test_discover_uses_manual_override_without_starting_server(self):
        with patch("sys.argv", ["agent-shuttle", "discover", "--opencode-command", "C:/tools/opencode.exe"]), \
             patch("agent_shuttle.cli.discover_harnesses", return_value={"opencode": "C:/tools/opencode.exe"}) as discover, \
             patch("builtins.print") as output:
            main()
        self.assertEqual(discover.call_args.args[0], {"opencode": "C:/tools/opencode.exe"})
        self.assertEqual(json.loads(output.call_args.args[0]), {"opencode": "C:/tools/opencode.exe"})

    def test_serve_profile_uses_registry_and_profile_name(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "profile.json"
            profile.write_text(json.dumps({
                "id": "claude-local", "runtime": "claude_code", "provider": "ollama",
                "workspace": ".", "default_model": "test", "allowed_models": ["test"],
            }), encoding="utf-8")
            with patch("sys.argv", ["agent-shuttle", "serve", "profile", "--profile", str(profile), "--port", "8767"]), \
                 patch("agent_shuttle.cli._run_server") as run:
                main()
            self.assertEqual(run.call_args.args[1], 8767)
            paths = {route.path for route in run.call_args.args[0].routes}
            self.assertIn("/shuttle/info", paths)
            self.assertIn("/shuttle/identity", paths)

    def test_profile_required_for_profile_runtime(self):
        with patch("sys.argv", ["agent-shuttle", "serve", "profile", "--port", "8767"]):
            with self.assertRaises(SystemExit):
                main()

    def test_profile_workspace_can_be_overridden_for_consumer_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile_path = root / "profile.json"
            project = root / "consumer"
            project.mkdir()
            profile_path.write_text(json.dumps({
                "id": "claude-local", "runtime": "claude_code", "provider": "ollama",
                "workspace": "C:/old-machine/missing-project", "default_model": "test",
                "allowed_models": ["test"],
            }), encoding="utf-8")
            with patch("sys.argv", ["agent-shuttle", "serve", "profile", "--profile",
                                    str(profile_path), "--workspace", str(project), "--port", "8768"]), \
                 patch("agent_shuttle.cli.build_profile") as build, \
                 patch("agent_shuttle.cli._run_server"):
                build.return_value = (object(), object())
                main()
            self.assertEqual(build.call_args.args[0].workspace, project.resolve())

    def test_ask_forwards_model_effort_and_policy(self):
        result = SimpleNamespace(state="TASK_STATE_COMPLETED", task_id="1", text="ok")
        with patch("sys.argv", ["agent-shuttle", "ask", "http://127.0.0.1:8767", "hello",
                                "--model", "test", "--reasoning-effort", "high",
                                "--tool-policy", "no_tools"]), \
             patch("agent_shuttle.cli.ShuttleClient.ask", new_callable=AsyncMock, return_value=result) as ask, \
             patch("builtins.print"):
            main()
        self.assertEqual(ask.call_args.kwargs["tool_policy"], "no_tools")
        self.assertEqual(ask.call_args.kwargs["model"], "test")

    def test_info_prints_json(self):
        with patch("sys.argv", ["agent-shuttle", "info", "http://127.0.0.1:8767"]), \
             patch("agent_shuttle.cli.ShuttleClient.info", new_callable=AsyncMock,
                   return_value={"models": ["local"]}) as info, \
             patch("builtins.print") as output:
            main()
        self.assertEqual(info.call_args.args[0], "http://127.0.0.1:8767")
        self.assertEqual(json.loads(output.call_args.args[0]), {"models": ["local"]})

    def test_legacy_serve_backends_and_invalid_profile_option(self):
        for agent, extra in (("codex", []), ("antigravity", []),
                             ("antigravity", ["--agy-mode", "sdk"])):
            with self.subTest(agent=agent, extra=extra), tempfile.TemporaryDirectory() as folder, \
                 patch("sys.argv", ["agent-shuttle", "serve", agent,
                                    "--workspace", folder, "--port", "8765", *extra]), \
                 patch("agent_shuttle.cli._run_server") as run:
                main()
                self.assertEqual(run.call_args.args[1], 8765)
        with patch("sys.argv", ["agent-shuttle", "serve", "codex", "--port", "8765",
                                "--profile", "profile.json"]):
            with self.assertRaises(SystemExit):
                main()

    def test_antigravity_full_permissions_are_explicit_server_opt_in(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("sys.argv", ["agent-shuttle", "serve", "antigravity", "--port", "8766",
                                "--workspace", folder, "--agy-dangerously-skip-permissions"]), \
             patch("agent_shuttle.registry.AntigravityCliBackend") as backend, \
             patch("agent_shuttle.cli._run_server") as run:
            main()
        self.assertTrue(run.call_args.args[0].routes)
        self.assertTrue(backend.call_args.kwargs["dangerously_skip_permissions"])

    def test_antigravity_turn_timeout_is_configurable_for_server(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("sys.argv", ["agent-shuttle", "serve", "antigravity", "--port", "8766",
                                "--workspace", folder, "--agy-turn-timeout-seconds", "42"]), \
             patch("agent_shuttle.registry.AntigravityCliBackend") as backend, \
             patch("agent_shuttle.cli._run_server"):
            main()
        self.assertEqual(backend.call_args.kwargs["turn_timeout_seconds"], 42)

    def test_antigravity_full_permissions_flag_rejected_for_other_backends(self):
        for agent, extra in (("codex", []), ("profile", []),
                             ("antigravity", ["--agy-mode", "sdk"])):
            with self.subTest(agent=agent, extra=extra), tempfile.TemporaryDirectory() as folder, \
                 patch("sys.argv", ["agent-shuttle", "serve", agent, "--port", "8765",
                                    "--workspace", folder, *extra,
                                    "--agy-dangerously-skip-permissions"]):
                with self.assertRaises(SystemExit):
                    main()


if __name__ == "__main__":
    unittest.main()
