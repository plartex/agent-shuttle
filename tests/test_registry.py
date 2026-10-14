import sys
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from agent_shuttle.profiled import ProfiledBackend, ProfiledInfo
from agent_shuttle.profiles import AgentProfile
from agent_shuttle.registry import build_profile, build_builtin


class RegistryTests(unittest.TestCase):
    def test_codex_factory_freezes_one_command_for_backend_and_info(self):
        with patch("agent_shuttle.registry.codex_command", return_value=(sys.executable,)) as resolve:
            backend, info = build_builtin("codex", Path.cwd(), command="chosen")
        resolve.assert_called_once_with("chosen")
        self.assertEqual(backend.command, info.command)
        self.assertEqual(backend.command, (sys.executable,))

    def test_builds_both_new_runtimes_without_starting_a_model(self):
        with tempfile.TemporaryDirectory() as workspace:
            for runtime in ("opencode", "claude_code", "acp"):
                with self.subTest(runtime=runtime):
                    config = {"id": runtime, "runtime": runtime, "workspace": workspace}
                    if runtime == "acp":
                        config["command"] = ["agent", "acp"]
                    else:
                        config.update(provider="ollama", default_model="test", allowed_models=["test"])
                    profile = AgentProfile.from_mapping(config)
                    backend, info = build_profile(profile)
                    self.assertIsInstance(backend, ProfiledBackend)
                    self.assertIsInstance(info, ProfiledInfo)
                    self.assertEqual(backend.profile.id, runtime)


if __name__ == "__main__":
    unittest.main()
