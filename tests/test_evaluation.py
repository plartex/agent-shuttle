import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge.evaluation import (
    EvaluationService,
    EvaluationTarget,
    format_text_report,
    load_code_smells_profile,
)
from agent_bridge.evaluation.batching import plan_batches
from agent_bridge.evaluation.prompting import MAX_INLINE_FILE_BYTES, build_evaluation_prompt
from agent_bridge.evaluation.providers import FakeAgentProvider
from agent_bridge.evaluation.providers.agent_bridge import AgentBridgeProvider
from agent_bridge.evaluation.providers.base import ProviderResponse
from agent_bridge.evaluation.models import CheckResult
from agent_bridge.evaluation.scoring import summarize


def request_from_prompt(prompt: str) -> dict:
    payload = prompt.split("REQUEST_DATA_JSON\n", 1)[1].split("\nEND_REQUEST_DATA_JSON", 1)[0]
    return json.loads(payload)


def response_for(prompt: str, result_factory) -> str:
    request = request_from_prompt(prompt)
    return json.dumps(
        {
            "schema_version": "1.0",
            "batch_id": request["batch_id"],
            "results": [result_factory(rule) for rule in request["rules"]],
        },
        ensure_ascii=False,
    )


class CatalogAndBatchingTest(unittest.TestCase):
    def test_partial_transport_failure_has_no_quality_score(self):
        summary = summarize((
            CheckResult(rule_id="ok", status="passed", severity="medium"),
            CheckResult(rule_id="error", status="error", severity="high", reason="denied"),
        ))
        self.assertIsNone(summary.quality_score)
        self.assertEqual(summary.assessment_coverage, 50.0)

    def test_bundled_catalog_contains_all_rules(self):
        profile = load_code_smells_profile()
        self.assertEqual(len(profile.rules), 80)
        self.assertEqual(len({rule.id for rule in profile.rules}), 80)
        self.assertEqual(profile.rules[0].id, "long_method")
        self.assertEqual(profile.rules[-1].id, "duplicated_derived_state")

    def test_batches_are_bounded_and_complete(self):
        rules = load_code_smells_profile().rules
        batches = plan_batches(rules, batch_size=7)
        self.assertTrue(batches)
        self.assertTrue(all(1 <= len(batch) <= 7 for batch in batches))
        flattened = [rule.id for batch in batches for rule in batch]
        self.assertCountEqual(flattened, [rule.id for rule in rules])

    def test_unknown_rule_is_rejected_before_agent_run(self):
        with self.assertRaisesRegex(ValueError, "Unknown rule ids"):
            load_code_smells_profile(rule_ids=["does_not_exist"])

    def test_file_target_is_inlined_for_fixed_workspace_agents(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "service.py"
            path.write_text("def answer():\n    return 42\n", encoding="utf-8")
            target = EvaluationTarget.file(path)
            rule = load_code_smells_profile(rule_ids=["long_method"]).rules
            payload = request_from_prompt(build_evaluation_prompt("batch-0001", target, rule))

        self.assertEqual(payload["target"]["kind"], "file")
        self.assertEqual(payload["target"]["path"], str(path.resolve()))
        self.assertEqual(payload["target"]["language"], "py")
        self.assertEqual(payload["target"]["content"], "def answer():\n    return 42\n")

    def test_evaluation_prompt_forbids_tools_and_external_file_context(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "service.py"
            path.write_text("value = 42\n", encoding="utf-8")
            target = EvaluationTarget.file(path)
            rule = load_code_smells_profile(rule_ids=["cyclic_dependency"]).rules
            prompt = build_evaluation_prompt("batch-0001", target, rule)

        self.assertIn("closed-book, tool-free evaluation", prompt)
        self.assertIn("Never invoke tools", prompt)
        self.assertIn("analyze only target.content", prompt)

    def test_oversized_file_fails_before_agent_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.py"
            path.write_bytes(b"x" * (MAX_INLINE_FILE_BYTES + 1))
            target = EvaluationTarget.file(path)
            rule = load_code_smells_profile(rule_ids=["long_method"]).rules
            with self.assertRaisesRegex(ValueError, "inline limit"):
                build_evaluation_prompt("batch-0001", target, rule)


class EvaluationServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_project_target_rejected_before_model_call_for_fixed_workspace_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = AgentBridgeProvider("http://127.0.0.1:1", "antigravity")
            with self.assertRaisesRegex(ValueError, "No model calls were made"):
                await EvaluationService(provider).evaluate(
                    EvaluationTarget.project(folder),
                    load_code_smells_profile(rule_ids=["long_method"]),
                )

    async def test_debug_trace_includes_batch_progress_and_actual_usage(self):
        profile = load_code_smells_profile(rule_ids=["long_method"])
        emitted = []

        def handler(prompt, workspace, model):
            return ProviderResponse(
                response_for(prompt, lambda rule: {
                    "rule_id": rule["id"], "status": "passed", "confidence": 1.0, "evidence": [],
                }),
                {"input_tokens": 1234, "output_tokens": 56, "total_tokens": 1290},
            )

        report = await EvaluationService(FakeAgentProvider(handler)).evaluate(
            EvaluationTarget.snippet("def small(): return 1"),
            profile,
            debug=True,
            on_debug_event=emitted.append,
        )
        events = [entry["event"] for entry in emitted]
        self.assertEqual(events, [
            "run_started", "batch_started", "agent_request", "agent_response",
            "validated", "batch_finished", "run_finished",
        ])
        self.assertEqual(emitted[-1]["usage_totals"]["input_tokens"], 1234)
        self.assertEqual(report.to_dict()["debug_trace"], emitted)

    async def test_model_and_reasoning_effort_reach_provider_and_report(self):
        profile = load_code_smells_profile(rule_ids=["long_method"])

        def handler(prompt, workspace, model):
            return response_for(
                prompt,
                lambda rule: {
                    "rule_id": rule["id"],
                    "status": "passed",
                    "confidence": 1.0,
                    "evidence": [],
                },
            )

        provider = FakeAgentProvider(handler)
        report = await EvaluationService(provider).evaluate(
            EvaluationTarget.snippet("def small(): return 1", language="python"),
            profile,
            model="chosen-model",
            reasoning_effort="high",
        )

        self.assertEqual(provider.calls[0][2:], ("chosen-model", "high"))
        self.assertEqual(report.provider.model, "chosen-model")
        self.assertEqual(report.provider.reasoning_effort, "high")

    async def test_failed_and_passed_rules_produce_weighted_score(self):
        profile = load_code_smells_profile(rule_ids=["long_method", "large_class"])

        def handler(prompt, workspace, model):
            def result(rule):
                if rule["id"] == "long_method":
                    return {
                        "rule_id": rule["id"],
                        "status": "failed",
                        "confidence": 0.96,
                        "evidence": [
                            {
                                "path": None,
                                "start_line": 1,
                                "end_line": 3,
                                "excerpt": "def process_order():",
                                "reason": "The function combines several unrelated responsibilities.",
                            }
                        ],
                    }
                return {
                    "rule_id": rule["id"],
                    "status": "passed",
                    "confidence": 0.91,
                    "evidence": [],
                }

            return response_for(prompt, result)

        provider = FakeAgentProvider(handler)
        target = EvaluationTarget.snippet(
            "def process_order():\n    validate()\n    charge()\n    notify()\n",
            language="python",
        )
        report = await EvaluationService(provider).evaluate(target, profile, batch_size=10)

        self.assertEqual(report.summary.total, 2)
        self.assertEqual(report.summary.passed, 1)
        self.assertEqual(report.summary.failed, 1)
        self.assertEqual(report.summary.findings, 1)
        self.assertEqual(report.summary.quality_score, 60.0)  # high passed / (high + medium)
        self.assertEqual(report.summary.assessment_coverage, 100.0)
        self.assertEqual([result.rule_id for result in report.results], ["long_method", "large_class"])
        self.assertFalse(report.warnings)
        self.assertEqual(len(provider.calls), 2)  # method and class scopes are separate batches
        text = format_text_report(report)
        self.assertIn("1/2 PASSED", text)
        self.assertIn("Code quality: 60.0%", text)
        self.assertIn("long_method", text)

    async def test_invalid_json_is_repaired_once(self):
        profile = load_code_smells_profile(rule_ids=["long_method"])
        attempts = 0

        def handler(prompt, workspace, model):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return "```json\nnot valid\n```"
            return response_for(
                prompt,
                lambda rule: {
                    "rule_id": rule["id"],
                    "status": "passed",
                    "confidence": 0.8,
                    "evidence": [],
                },
            )

        provider = FakeAgentProvider(handler)
        report = await EvaluationService(provider).evaluate(
            EvaluationTarget.snippet("def small():\n    return 1\n", language="python"),
            profile,
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(report.summary.passed, 1)
        self.assertEqual(report.summary.errors, 0)

    async def test_evidence_outside_project_turns_batch_into_error(self):
        profile = load_code_smells_profile(rule_ids=["long_method"])

        def handler(prompt, workspace, model):
            return response_for(
                prompt,
                lambda rule: {
                    "rule_id": rule["id"],
                    "status": "failed",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "path": "../outside.py",
                            "start_line": 1,
                            "end_line": 1,
                            "excerpt": "outside",
                            "reason": "Invalid out-of-scope evidence.",
                        }
                    ],
                },
            )

        provider = FakeAgentProvider(handler)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "inside.py").write_text("print('inside')\n", encoding="utf-8")
            report = await EvaluationService(provider).evaluate(EvaluationTarget.project(root), profile)

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(report.summary.errors, 1)
        self.assertEqual(report.summary.assessment_coverage, 0.0)
        self.assertIsNone(report.summary.quality_score)
        self.assertIn("outside the project target", report.results[0].reason)

    async def test_skipped_requires_reason(self):
        profile = load_code_smells_profile(rule_ids=["cyclic_dependency"])

        def handler(prompt, workspace, model):
            return response_for(
                prompt,
                lambda rule: {
                    "rule_id": rule["id"],
                    "status": "skipped",
                    "confidence": 1.0,
                    "evidence": [],
                    "reason": "A standalone snippet has no module dependency graph.",
                },
            )

        report = await EvaluationService(FakeAgentProvider(handler)).evaluate(
            EvaluationTarget.snippet("x = 1", language="python"),
            profile,
        )
        self.assertEqual(report.summary.skipped, 1)
        self.assertEqual(report.summary.assessment_coverage, 0.0)


if __name__ == "__main__":
    unittest.main()
