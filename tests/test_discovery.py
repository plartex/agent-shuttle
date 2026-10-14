import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_shuttle.discovery import discover_harnesses, codex_command
from agent_shuttle.backends import _codex_config


class DiscoveryTests(unittest.TestCase):
    def test_precedence_and_real_commands(self):
        with patch.dict(os.environ, {"AGENT_SHUTTLE_CODEX_COMMAND": "environment"}, clear=True), \
             patch("agent_shuttle.discovery.shutil.which", side_effect=lambda cmd, **kw: str(Path(cmd).absolute())):
            self.assertEqual(discover_harnesses({"codex": "explicit"})["codex"], str(Path("explicit").absolute()))
            self.assertEqual(discover_harnesses()["codex"], str(Path("environment").absolute()))

    def test_invalid_explicit_command_never_falls_back(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("agent_shuttle.discovery.shutil.which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                discover_harnesses({"codex": "missing-agent"})
            with patch.dict(os.environ, {"AGENT_SHUTTLE_AGY_COMMAND": "missing-agent"}):
                with self.assertRaises(FileNotFoundError):
                    discover_harnesses()

    def test_sdk_alone_does_not_supply_a_harness(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("agent_shuttle.discovery.shutil.which", return_value=None):
            self.assertEqual(discover_harnesses(), {})
            with self.assertRaises(FileNotFoundError):
                codex_command()

    def test_excludes_bundled_sdk_directories_only_for_automatic_search(self):
        bundle = Path.cwd() / "site-packages" / "codex_cli_bin"
        external = str(Path.cwd() / "user-bin")
        with patch.dict(os.environ, {}, clear=True), \
             patch("agent_shuttle.discovery.importlib.util.find_spec", return_value=SimpleNamespace(origin=str(bundle / "__init__.py"))), \
             patch("agent_shuttle.discovery.os.get_exec_path", return_value=[str(bundle / "bin"), str(bundle / "codex-path"), external]), \
             patch("agent_shuttle.discovery.shutil.which", return_value=None) as which:
            discover_harnesses()
            self.assertEqual(which.call_args_list[0].kwargs["path"], external)

    def test_explicit_unicode_path_and_sdk_override(self):
        with tempfile.TemporaryDirectory(prefix="harness space ") as folder:
            executable = Path(folder) / "агент.exe"
            executable.touch()
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(codex_command(str(executable)), (str(executable),))

    def test_unknown_harness_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown harnesses"):
            discover_harnesses({"unknown": "anything"})

    @unittest.skipUnless(os.name == "nt", "Windows npm launcher")
    def test_npm_launcher_and_missing_node(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shim = root / "codex.cmd"
            shim.touch()
            script = root / "node_modules/@openai/codex/bin/codex.js"
            script.parent.mkdir(parents=True)
            script.touch()
            with patch("agent_shuttle.discovery.shutil.which", return_value=None):
                with self.assertRaisesRegex(FileNotFoundError, "npm shim"):
                    codex_command(str(shim))
                node = root / "node.exe"
                node.touch()
                command = codex_command(str(shim))
            self.assertEqual(command, (str(node), str(script)))
            config = _codex_config(command, {})
            self.assertEqual(config.launch_args_override, (*command, "app-server", "--listen", "stdio://"))

    def test_native_config_does_not_use_sdk_default(self):
        config = _codex_config((sys.executable,), {})
        self.assertEqual(config.codex_bin, sys.executable)
        self.assertIsNone(config.launch_args_override)
