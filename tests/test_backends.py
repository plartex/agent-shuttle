import json
import unittest

from agent_bridge.backends import _decode_agy_result, _decode_agy_usage


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

    def test_extracts_only_nonnegative_token_counters(self) -> None:
        payload = {"usage": {
            "input_tokens": 123, "output_tokens": 7, "total_tokens": 130,
            "thinking_tokens": -1, "cache_read_tokens": True, "other": 99,
        }}
        self.assertEqual(
            _decode_agy_usage(json.dumps(payload).encode()),
            {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130},
        )


if __name__ == "__main__":
    unittest.main()
