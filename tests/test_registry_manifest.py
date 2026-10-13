"""Offline registry publication and cold-selection fixture contracts."""
import json
import tomllib
import unittest
from pathlib import Path

import jsonschema

from agent_shuttle.presentation import REGISTRY_NAME, REGISTRY_DESCRIPTION, VERSION


class RegistryManifestTests(unittest.TestCase):
    def test_manifest_matches_official_schema_release_and_pypi_ownership(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "server.json").read_text(encoding="utf-8"))
        schema = json.loads((root / "tests/fixtures/mcp-server-schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(manifest, schema)
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], REGISTRY_NAME)
        self.assertEqual(manifest["description"], REGISTRY_DESCRIPTION)
        self.assertEqual(manifest["version"], config["project"]["version"])
        self.assertEqual(manifest["version"], VERSION)
        package = manifest["packages"][0]
        self.assertEqual(package["version"], VERSION)
        self.assertEqual(package["identifier"], "agent-shuttle")
        self.assertEqual(package["packageArguments"], [{"type": "positional", "value": "mcp"}])
        self.assertIn(f"mcp-name: {REGISTRY_NAME}", (root / "README.md").read_text(encoding="utf-8"))

    def test_corpus_has_balanced_languages_and_required_case_types(self):
        path = Path(__file__).parent / "fixtures/discovery_queries.json"
        cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
        self.assertEqual(len(cases), 24)
        self.assertEqual(sum(case["delegation_intent"] for case in cases), 16)
        self.assertEqual({case["language"] for case in cases}, {"ru", "en"})
        self.assertEqual(len({case["id"] for case in cases}), 24)
