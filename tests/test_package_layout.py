"""The distribution has one implementation package in a standard src layout."""

import unittest
from pathlib import Path

import agent_shuttle


class PackageLayoutTests(unittest.TestCase):
    def test_only_one_package_lives_under_src(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "src" / "agent_shuttle" / "__init__.py").is_file())
        self.assertFalse((root / "agent_shuttle").exists())
        self.assertFalse((root / "agent_bridge").exists())

    def test_public_package_comes_from_src(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(Path(agent_shuttle.__file__).resolve().parent,
                         root / "src" / "agent_shuttle")
        self.assertIs(agent_shuttle.ShuttleClient, agent_shuttle.BridgeClient)


if __name__ == "__main__":
    unittest.main()
