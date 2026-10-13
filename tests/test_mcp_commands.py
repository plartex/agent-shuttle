"""User-visible command outcomes and no-tools setup boundaries."""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_shuttle.cli import main
from agent_shuttle.mcp_transport import run_server, TOKEN_ENV
from agent_shuttle.mcp_gateway import run_gateway
from agent_shuttle.agent_catalog import inventory


class McpCommandTests(unittest.TestCase):
    def test_setup_preview_cli_is_machine_readable_and_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            output = io.StringIO()
            with patch.object(sys, "argv", ["agent-shuttle", "mcp", "setup", "--format", "mcp-json", "--config", str(path), "--workspace", folder]), contextlib.redirect_stdout(output):
                main()
            result = json.loads(output.getvalue())
            self.assertFalse(result["applied"])
            self.assertFalse(path.exists())

    def test_doctor_json_reports_failure_without_an_agent_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            output = io.StringIO()
            with patch.object(sys, "argv", ["agent-shuttle", "mcp", "doctor", "--format", "mcp-json", "--config", str(Path(folder)/"missing.json"), "--json"]), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as exited:
                    main()
            self.assertEqual(exited.exception.code, 1)
            self.assertEqual(json.loads(output.getvalue())["connection"], "unverified")

    def test_http_requires_a_token_and_rejects_an_inherited_worker(self):
        from agent_shuttle.mcp_server import mcp
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                run_server(mcp, ["--transport", "streamable-http"])
        with patch.dict(os.environ, {TOKEN_ENV: "x"*40, "AGENT_SHUTTLE_PARENT_CONTEXT": "worker"}), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "nested_dispatch_disabled"):
                run_server(mcp, ["--transport", "streamable-http"])
            self.assertNotIn(TOKEN_ENV, os.environ)

    def test_missing_docker_is_diagnosed_before_launch(self):
        with patch.dict(os.environ, {}, clear=True), patch("agent_shuttle.mcp_gateway.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Docker CLI is not installed"):
                asyncio.run(run_gateway("default"))

    def test_profile_command_and_global_mapping_errors_are_reported_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)/"agent.json"
            profile.write_text(json.dumps({"id":"auditor", "runtime":"acp", "workspace":folder, "command":[sys.executable]}))
            mapping = {"auditor":{"profile":str(profile)}, "codex":{"url":"http://127.0.0.1:8765"}, "remote":"http://127.0.0.1:8999"}
            with patch.dict(os.environ, {"AGENT_SHUTTLE_AGENTS_JSON":json.dumps(mapping)}):
                items = {item["agent_id"]:item for item in inventory()["agents"]}
            self.assertTrue(items["auditor"]["command_available"])
            self.assertEqual(items["codex"]["configuration_status"], "configured")
            self.assertIsNone(items["remote"]["command_available"])
            with patch.dict(os.environ, {"AGENT_SHUTTLE_AGENTS_JSON":"[invalid"}):
                self.assertTrue(inventory()["configuration_error"])
