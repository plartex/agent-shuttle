import tempfile
import unittest

from agent_shuttle.profiled import ProfiledBackend, ProfiledInfo
from agent_shuttle.profiles import AgentProfile
from agent_shuttle.registry import build_profile


class RegistryTests(unittest.TestCase):
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
