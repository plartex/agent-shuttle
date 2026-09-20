import json
import unittest

from agent_bridge.backends import _decode_agy_result


class AntigravityCliBackendTests(unittest.TestCase):
    @staticmethod
    def decode(payload: dict[str, object], stderr: str = "") -> str:
        return _decode_agy_result(json.dumps(payload).encode(), stderr.encode())

    def test_returns_non_empty_success_response(self) -> None:
        self.assertEqual(self.decode({"status": "SUCCESS", "response": "ok"}), "ok")

    def test_rejects_empty_success_response(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "soft-denied.*ViewFile"):
            self.decode(
                {"status": "SUCCESS", "response": ""},
                'Tool "ViewFile" requires confirmation',
            )

    def test_rejects_invalid_json_with_stderr_context(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invalid JSON.*backend unavailable"):
            _decode_agy_result(b"not-json", b"backend unavailable")


if __name__ == "__main__":
    unittest.main()
