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
from agent_bridge.evaluation.providers import FakeAgentProvider


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


class EvaluationServiceTest(unittest.IsolatedAsyncioTestCase):
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
