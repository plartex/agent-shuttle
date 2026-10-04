"""The distribution exposes only the Agent Shuttle package and commands."""

import tomllib
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch


class AgentShuttleBrandingTests(unittest.TestCase):
    def test_module_entry_point_uses_the_canonical_cli(self):
        with patch("agent_shuttle.cli.main") as cli_main:
            runpy.run_module("agent_shuttle", run_name="__main__")
        cli_main.assert_called_once_with()

    def test_public_python_namespace(self):
        import agent_shuttle

        self.assertIs(agent_shuttle.ShuttleClient, agent_shuttle.BridgeClient)
        self.assertTrue(callable(agent_shuttle.connect_harness))

    def test_mcp_server_advertises_new_name(self):
        from agent_shuttle.mcp_server import mcp

        self.assertEqual(mcp.name, "Agent Shuttle")

    def test_distribution_has_only_canonical_commands_and_src_layout(self):
        config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        self.assertEqual(config["project"]["name"], "agent-shuttle")
        self.assertEqual(config["tool"]["setuptools"]["packages"]["find"]["where"], ["src"])
        self.assertEqual(config["project"]["scripts"], {
            "agent-shuttle": "agent_shuttle.cli:main",
            "agent-shuttle-mcp": "agent_shuttle.mcp_server:main",
        })


if __name__ == "__main__":
    unittest.main()
