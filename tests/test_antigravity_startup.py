"""A listening Bridge must have verified Antigravity's account context."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from agent_shuttle.a2a_server import make_app
from agent_shuttle.backends import AntigravityAuthenticationError, AntigravityCliBackend
from agent_shuttle.info import AntigravityCliInfo


DENIED_PROFILE = (
    b"Failed to redirect output for CLI: open C:/Users/user/.gemini/antigravity-cli/log/cli.log: Access is denied.\n"
    b"error getting token source: You are not logged into Antigravity.\n"
    b"Error: Please sign in to view available models.\n"
)


class Process:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode

    async def communicate(self):
        return self.stdout, self.stderr


class AntigravityStartupTests(unittest.TestCase):
    def test_denied_profile_never_becomes_a_ready_server(self):
        # Replay the observed failure repeatedly; HTTP readiness must not hide it.
        for attempt in range(5):
            with self.subTest(attempt=attempt), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                app = make_app("antigravity", AntigravityCliBackend(root),
                               "http://127.0.0.1:8766", AntigravityCliInfo(root))
                with patch("agent_shuttle.info.asyncio.create_subprocess_exec",
                           return_value=Process(stderr=DENIED_PROFILE, returncode=1)) as spawn:
                    with self.assertRaisesRegex(AntigravityAuthenticationError, "outside the caller's sandbox"):
                        with TestClient(app):
                            self.fail("Bridge advertised readiness despite an inaccessible CLI profile")
                spawn.assert_called_once()

    def test_accessible_profile_is_checked_without_a_model_turn(self):
        envelope = json.dumps({"status": "SUCCESS", "command": {"data": {
            "models": [{"id": "gemini-3.8-flash-high"}],
        }}}).encode()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = make_app("antigravity", AntigravityCliBackend(root),
                           "http://127.0.0.1:8766", AntigravityCliInfo(root))
            with patch("agent_shuttle.info.asyncio.create_subprocess_exec",
                       return_value=Process(stdout=envelope)) as spawn:
                with TestClient(app) as client:
                    self.assertEqual(client.get("/bridge/identity").status_code, 200)
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.args[1:], ("--output-format", "json", "models"))

    def test_empty_model_catalog_does_not_advertise_readiness(self):
        envelope = b'{"status":"SUCCESS","command":{"data":{"models":[]}}}'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = make_app("antigravity", AntigravityCliBackend(root),
                           "http://127.0.0.1:8766", AntigravityCliInfo(root))
            with patch("agent_shuttle.info.asyncio.create_subprocess_exec",
                       return_value=Process(stdout=envelope)):
                with self.assertRaisesRegex(RuntimeError, "model catalog"):
                    with TestClient(app):
                        self.fail("An empty model catalog was accepted")


if __name__ == "__main__":
    unittest.main()
