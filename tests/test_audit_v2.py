"""Regression tests for reproduced audit failures. No live API in these tests."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from cqa.domain import ValidationError, aggregate, normalize_conversation, validate_decisions, materialize_evidence, validate_rubric
from cqa.metrics import diagnostics
from cqa.providers import ChatProvider, NoRedirect, ProviderError
from cqa.rubric import official_rubric
from cqa.server import Application
import test_core as core
from test_core import conversation


def row(cid, value=10):
    return {"conversation_id": cid, "criteria": [{"criterion_id": "dialogue", "value": value}]}


class MeasurementIntegrityTests(unittest.TestCase):
    def test_same_id_with_different_source_does_not_match_truth(self):
        prediction = {**row("a"), "transcript_hash": "new-source"}
        label = {**row("a"), "transcript_hash": "old-source"}
        with self.assertRaises(ValidationError): self.criterion([prediction], [label])

    def test_repeats_cannot_combine_different_known_source_versions(self):
        predictions = [{**row("a"), "transcript_hash": value} for value in ("source-one", "source-two")]
        with self.assertRaises(ValidationError): self.criterion(predictions, [row("a")])

    def criterion(self, predictions, labels):
        report = diagnostics(predictions, {"results": labels})["runs"][0]
        return next(c for c in report["per_criterion"] if c["criterion_id"] == "dialogue")

    def test_missing_whole_prediction_counts_in_exact_denominator(self):
        c = self.criterion([row("a")], [row("a"), row("b")])
        self.assertEqual(c["n_compared"], 2)
        self.assertEqual(c["exact_match"], .5)

    def test_empty_predictions_report_zero_exact(self):
        self.assertEqual(self.criterion([], [row("a")])["exact_match"], 0)

    def test_repeated_easy_case_does_not_outweigh_difficult_case(self):
        c = self.criterion([row("a")]*20+[row("b", 0)], [row("a"), row("b")])
        self.assertEqual(c["exact_match"], .5)
        self.assertEqual(c["mae_raw"], 5)

    def test_invalid_scales_types_and_nonfinite_values_rejected(self):
        for value in [999, 5, True, "10", float("nan"), float("inf"), None]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.criterion([row("a", value)], [row("a")])

    def test_truth_is_validated_as_well_as_predictions(self):
        with self.assertRaises(ValidationError): self.criterion([row("a")], [row("a", 999)])

    def test_missing_critical_positive_penalizes_recall(self):
        truth = {"results": [{"conversation_id": "a", "criteria": [{"criterion_id": "critical", "value": True}]}]}
        r = diagnostics([], truth)["runs"][0]["critical"]
        self.assertEqual(r["fn"], 1)
        self.assertEqual(r["recall"], 0)

    def test_total_missing_does_not_disappear_from_exact_metric(self):
        a, b = row("a"), row("b")
        a["score"] = b["score"] = {"final_score": 80}
        r = diagnostics([a], {"results": [a, b]})["runs"][0]["total"]
        self.assertEqual(r["exact_match"], .5)
        self.assertEqual(r["numeric_coverage"], .5)
        self.assertEqual(r["mae"], 0)

    def test_code_revisions_are_not_pooled(self):
        predictions = [row("a"), row("a")]
        for i, r in enumerate(predictions): r["provenance"] = {"code_hash": str(i)}
        self.assertEqual(len(diagnostics(predictions, {"results": [row("a")]})["runs"]), 2)

    def test_human_tag_without_complete_rubric_not_accuracy_evidence(self):
        r = diagnostics([row("a")], {"provenance": {"label_source": "human"}, "results": [row("a")]})
        self.assertFalse(r["accuracy_claim_allowed"])

    def test_complete_rubric_total_recomputed(self):
        refs = core.ReferenceContractTests().refs()
        values = []
        for c in refs["criteria"]:
            a = c["answer"]
            values.append({"criterion_id": c["criterion_id"], "value": None if a=="N/A" else a=="YES" if a in ("YES","NO") else int(a)})
        truth = {"conversation_id": "a", "criteria": values, "score": {"final_score": -1}}
        with self.assertRaises(ValidationError): diagnostics([truth], {"results": [truth]})
        truth["score"]["final_score"] = aggregate(official_rubric(), refs["criteria"])["total"]
        bad = copy.deepcopy(truth); bad["score"]["final_score"] -= 1
        with self.assertRaises(ValidationError): diagnostics([bad], {"results": [truth]})


class RepairAndBoundaryTests(unittest.TestCase):
    def test_provider_json_validation_400_gets_one_repair(self):
        good, calls = self.provider([core.ReferenceContractTests().refs()])
        valid_open = good.opener
        first = []
        def open_response(*a, **k):
            if not first:
                first.append(1)
                raise urllib.error.HTTPError("test",400,"bad schema",{},io.BytesIO(json.dumps({"error":{"code":"json_validate_failed","failed_generation":"fixture-only-invalid-json"}}).encode()))
            return valid_open(*a, **k)
        good.opener = open_response
        _, meta = good.evaluate(official_rubric(), conversation())
        self.assertEqual(meta["attempts"], 2); self.assertEqual(meta["validation_repairs"], 1)
        self.assertFalse(meta["token_usage_complete"])
        self.assertEqual(meta["rejected_responses"][0]["raw_response"]["code"], "json_validate_failed")

    def test_unknown_400_is_not_retried_or_echoed(self):
        calls=[]
        def fail(*a,**k):
            calls.append(1)
            raise urllib.error.HTTPError("test",400,"bad request",{},io.BytesIO(b'{"error":{"code":"context_limit","message":"PRIVATE FIXTURE TEXT"}}'))
        provider=ChatProvider("groq","qwen/qwen3.8-27b",key="gsk_test",free_plan_confirmed=True,opener=fail)
        with self.assertRaises(ProviderError) as e:provider.evaluate(official_rubric(),conversation())
        self.assertEqual(len(calls),1);self.assertNotIn("PRIVATE",str(e.exception))

    def test_three_rate_limits_then_fourth_response_within_budget(self):
        provider, requests=self.provider([core.ReferenceContractTests().refs()]); valid_open=provider.opener; attempts=[]
        def throttled(*a,**k):
            attempts.append(1)
            if len(attempts)<4:raise urllib.error.HTTPError("test",429,"limited",{"retry-after":"0"},None)
            return valid_open(*a,**k)
        provider.opener=throttled
        with patch("cqa.providers.time.sleep") as sleep:
            _,meta=provider.evaluate(official_rubric(),conversation())
        self.assertEqual(meta["attempts"],4);self.assertEqual(sleep.call_count,3)
        for call in sleep.call_args_list:self.assertGreaterEqual(call.args[0],.5)

    def test_late_operator_turn_cannot_support_official_positive_contact(self):
        raw = core.ReferenceContractTests().refs()
        contact = next(c for c in raw["criteria"] if c["criterion_id"]=="contact")
        contact["evidence_turns"] = ["t0004"]
        with self.assertRaises(ValidationError): validate_decisions(materialize_evidence(raw, conversation()), official_rubric(), conversation())

    def test_no_critical_violation_requires_full_coverage(self):
        raw = core.ReferenceContractTests().refs()
        critical = next(c for c in raw["criteria"] if c["criterion_id"]=="critical")
        critical["coverage"]["end"] = "t0003"
        critical["evidence_turns"] = ["t0002"]
        with self.assertRaises(ValidationError): validate_decisions(materialize_evidence(raw, conversation()), official_rubric(), conversation())

    def test_replaced_contact_instruction_does_not_inherit_official_boundary(self):
        rubric = official_rubric()
        next(c for c in rubric["criteria"] if c["id"]=="contact")["instruction"] = "Оператор предложил дальнейшую помощь в любой реплике."
        rubric = validate_rubric(rubric)
        raw = core.ReferenceContractTests().refs()
        next(c for c in raw["criteria"] if c["criterion_id"]=="contact")["evidence_turns"] = ["t0004"]
        self.assertEqual(len(validate_decisions(materialize_evidence(raw, conversation()), rubric, conversation())), 8)

    def provider(self, outputs):
        requests = []
        def open_response(request, **kwargs):
            requests.append(json.loads(request.data))
            raw = outputs[min(len(requests)-1, len(outputs)-1)]
            class Response(io.BytesIO): headers = {}
            return Response(json.dumps({"model": "transport-fixture", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}).encode())
        return ChatProvider("groq", "qwen/qwen3.8-27b", key="gsk_test", free_plan_confirmed=True, opener=open_response), requests

    def test_invalid_scale_repair_uses_validator_and_counts_both_calls(self):
        valid = core.ReferenceContractTests().refs(); invalid = copy.deepcopy(valid)
        next(c for c in invalid["criteria"] if c["criterion_id"]=="consultation")["answer"] = "7"
        p, requests = self.provider([invalid, valid])
        _, meta = p.evaluate(official_rubric(), conversation())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["messages"][:2], requests[1]["messages"][:2])
        self.assertIn("валидатором", requests[1]["messages"][-1]["content"])
        self.assertEqual(meta["validation_repairs"], 1)
        self.assertEqual(meta["input_tokens"], 200)
        self.assertEqual(len(meta["rejected_responses"]), 1)

    def test_persistent_bad_evidence_never_becomes_success(self):
        bad = core.ReferenceContractTests().refs(); bad["criteria"][0]["evidence_turns"] = ["t9999"]
        p, requests = self.provider([bad])
        with self.assertRaises(ProviderError) as e: p.evaluate(official_rubric(), conversation())
        self.assertEqual(len(requests), 2)
        self.assertEqual(e.exception.code, "invalid_response")
        json.dumps(e.exception.metadata)  # No cyclic trace on repeated failure.

    def test_rejected_answer_kept_privately_without_polluting_success(self):
        bad = core.ReferenceContractTests().refs(); bad["criteria"][0]["evidence_turns"] = ["t9999"]
        p, _ = self.provider([bad, core.ReferenceContractTests().refs()])
        base = Path(".local/test-runs"); base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            app = Application(data_dir=folder)
            try:
                conv = conversation(); rubric = official_rubric(); app.store.import_conversations([conv])
                raw, meta = p.evaluate(rubric, conv)
                saved = app.store.save_evaluation(conv, rubric, raw, meta)
                failures = app.store._list("failed_attempts")
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["raw_response"], bad)
                self.assertNotIn("rejected_responses", saved["provenance"])
            finally: app.store.close()

    def test_redirect_blocked_before_second_request(self):
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8081/v1/chat", data=b"private transcript", headers={"Authorization": "Bearer secret"})
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code), self.assertRaises(ProviderError):
                NoRedirect().redirect_request(req, None, code, "redirect", {}, "https://example.org/collect")

    def test_503_is_not_described_as_exhausted_daily_quota(self):
        def offline(*a, **k): raise urllib.error.HTTPError("test", 503, "unavailable", {"retry-after": "120"}, None)
        p = ChatProvider("groq", "qwen/qwen3.8-27b", key="gsk_test", free_plan_confirmed=True, opener=offline)
        with patch("cqa.providers.time.sleep") as sleep, self.assertRaises(ProviderError) as e:
            p.evaluate(official_rubric(), conversation())
        self.assertEqual(e.exception.code, "unavailable"); self.assertEqual(e.exception.retry_after, 120)
        self.assertNotIn("Квота API исчерпана", str(e.exception)); sleep.assert_not_called()
