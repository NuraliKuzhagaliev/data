"""Typed rubric, exact counterfactual arithmetic and Gemini transport contracts.
All provider responses in this module are injected fixtures, not live AI.
"""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from cqa.domain import ValidationError, aggregate, decode_model_json, materialize_evidence, validate_decisions, validate_rubric
from cqa.gemini import GeminiProvider
from cqa.prompts import official_guidance, output_schema, messages
from cqa.providers import ProviderError
from cqa.quality import compare_originals, review_priority
from cqa.rubric import official_rubric
from cqa.server import Application, normalize_gemini_key
from tools.quality_report import summarize
import test_core as core


def fixture_evaluation():
    conv = core.conversation(); rubric = official_rubric()
    raw = core.output(rubric, conv)
    answers = {c["criterion_id"]: c["answer"] for c in core.all_yes(rubric)}
    for c in raw["criteria"]:
        c["answer"] = answers[c["criterion_id"]]; c["ambiguous"] = False; c["limitations"] = []
    decisions = validate_decisions(raw, rubric, conv)
    return {"id":"fixture-original", "rubric":rubric, "criteria":decisions, "original_summary":aggregate(rubric,decisions),
            "transcript_hash":conv["hash"], "provenance":{"is_mock":False}}, conv


class CriterionContractTests(unittest.TestCase):
    def test_each_criterion_has_its_own_answer_scale(self):
        schema = output_schema(official_rubric(), core.conversation())
        criteria = schema["properties"]["criteria"]
        self.assertEqual(criteria["type"], "object"); self.assertFalse(criteria["additionalProperties"])
        answers = lambda cid: criteria["properties"][cid]["properties"]["answer"]["enum"]
        self.assertEqual(answers("consultation"), ["0","5","10","15","20","N/A"])
        self.assertEqual(answers("dialogue"), ["0","4","7","10"])
        self.assertEqual(answers("critical"), ["YES","NO"])
        self.assertNotIn("7", answers("consultation")); self.assertNotIn("N/A", answers("contact"))

    def test_map_materializes_exact_source_without_mutation(self):
        raw = core.ReferenceContractTests().refs()
        mapped = {"criteria":{c["criterion_id"]:{k:v for k,v in c.items() if k!="criterion_id"} for c in raw["criteria"]}}
        before = copy.deepcopy(mapped)
        result = materialize_evidence(mapped, core.conversation())
        self.assertEqual(mapped, before)
        self.assertEqual(len(validate_decisions(result, official_rubric(), core.conversation())),8)

    def test_map_key_and_embedded_id_cannot_conflict(self):
        with self.assertRaises(ValidationError): materialize_evidence({"criteria":{"contact":{"criterion_id":"critical"}}},core.conversation())

    def test_duplicate_json_keys_are_rejected_before_materialization(self):
        with self.assertRaises(ValidationError): decode_model_json('{"criteria":{"contact":{},"contact":{}}}')

    def test_custom_rule_does_not_inherit_official_guidance(self):
        rubric = official_rubric()
        next(c for c in rubric["criteria"] if c["id"]=="contact")["instruction"] = "Да, если оператор представился в любой реплике, не обязательно первой."
        rubric = validate_rubric(rubric)
        self.assertNotIn("contact", official_guidance(rubric))
        self.assertIn("dialogue", official_guidance(rubric))
        self.assertEqual(output_schema(rubric)["properties"]["criteria"]["properties"]["contact"]["properties"]["answer"]["enum"],["YES","NO"])

    def test_custom_numeric_scale_is_not_overridden(self):
        rubric = official_rubric();c=next(c for c in rubric["criteria"] if c["id"]=="consultation")
        c["levels"]=[{"score":i,"description":str(i)} for i in range(3)]
        rubric = validate_rubric(rubric)
        self.assertNotIn("consultation",official_guidance(rubric))
        self.assertEqual(output_schema(rubric)["properties"]["criteria"]["properties"]["consultation"]["properties"]["answer"]["enum"],["0","1","2","N/A"])

    def test_model_does_not_receive_weights_for_subjective_grading(self):
        rubric=official_rubric();payload=json.loads(messages(rubric,core.conversation())[1]["content"])
        self.assertTrue(all("weight" not in c and "section" not in c for c in payload["rubric"]["criteria"]))
        self.assertIn("consultation",payload["official_guidance"])


class ReviewPriorityTests(unittest.TestCase):
    def test_full_scale_impact_is_the_official_weight(self):
        evaluation,conv=fixture_evaluation();report=review_priority(evaluation,conv)
        values={r["criterion_id"]:r for r in report["items"]}
        self.assertEqual(values["identification"]["max_score_change"],20)
        self.assertEqual(values["contact"]["max_score_change"],10)
        self.assertEqual(values["critical"]["max_score_change"],0)
        self.assertFalse(report["is_accuracy_estimate"]);self.assertIsNone(report["calibrated_confidence"])

    def test_na_reweights_other_answers_not_just_raw_weight(self):
        evaluation,conv=fixture_evaluation()
        for c in evaluation["criteria"]:
            if c["criterion_id"] in ("discovery","identification","consultation","closure"):c["answer"]="N/A"
        report=review_priority(evaluation,conv)
        values={r["criterion_id"]:r for r in report["items"]}
        self.assertEqual(values["contact"]["max_score_change"],25)
        self.assertEqual(values["identification"]["max_score_change"],33.33)

    def test_gross_violation_has_priority_without_zeroing_total(self):
        evaluation,conv=fixture_evaluation()
        next(c for c in evaluation["criteria"] if c["criterion_id"]=="critical")["answer"]="YES"
        report=review_priority(evaluation,conv)
        self.assertEqual(report["current_total"],100)
        self.assertEqual(report["items"][0]["criterion_id"],"critical")

    def test_legacy_partial_coverage_is_visible_not_silently_fixed(self):
        evaluation,conv=fixture_evaluation()
        next(c for c in evaluation["criteria"] if c["criterion_id"]=="critical")["coverage"]={"start":"t0002","end":"t0002"}
        before=copy.deepcopy(evaluation);report=review_priority(evaluation,conv)
        self.assertEqual(evaluation,before)
        self.assertTrue(report["items"][0]["boundary_issue"])

    def test_repeated_different_answer_is_exposed_without_averaging(self):
        first,conv=fixture_evaluation();second=copy.deepcopy(first);second["id"]="fixture-repeat"
        next(c for c in second["criteria"] if c["criterion_id"]=="contact")["answer"]="NO"
        second["original_summary"]=aggregate(second["rubric"],second["criteria"])
        comparison=compare_originals(first,second)
        self.assertEqual(comparison["changed_criteria"],1);self.assertEqual(comparison["total_delta"],-10)
        self.assertEqual(first["original_summary"]["total"],100)

    def test_different_input_is_not_called_agreement(self):
        first,_=fixture_evaluation();second=copy.deepcopy(first);second["transcript_hash"]="other"
        self.assertEqual(compare_originals(first,second)["status"],"incompatible")


class GeminiContractTests(unittest.TestCase):
    def test_key_accepts_plain_quoted_and_environment_assignment(self):
        key="opaque-google-fixture-token-123456789"
        for value in (key,' "'+key+'" ',"GEMINI_API_KEY='"+key+"'","export GOOGLE_API_KEY=\""+key+'"',"$env:GEMINI_API_KEY = '"+key+"'"):
            with self.subTest(format=value[:12]):self.assertEqual(normalize_gemini_key(value),key)

    def test_key_rejects_foreign_credentials_and_header_injection(self):
        for key in ('gsk_fixture-key-123456789','sk-proj-fixture-123456789','abcdefgh123456789\r\nInjected: value','too short'):
            with self.subTest(kind=key[:8]),self.assertRaises(ValidationError):normalize_gemini_key(key)

    def response(self, raw=None, finish="STOP", empty=False):
        raw = core.ReferenceContractTests().refs() if raw is None else raw
        class Response(io.BytesIO):headers={}
        return Response(json.dumps({"modelVersion":"gemini-3.8-flash","candidates":[] if empty else [{"finishReason":finish,"content":{"parts":[{"text":json.dumps(raw)}]}}],
                                   "usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":30,"thoughtsTokenCount":10,"totalTokenCount":140}}).encode())

    def test_no_network_without_free_tier_confirmation(self):
        p=GeminiProvider(key="AIza_fixture_only",opener=lambda *a,**k:self.fail("network forbidden"))
        with self.assertRaises(ProviderError):p.evaluate(official_rubric(),core.conversation())

    def test_native_schema_header_and_usage(self):
        requests=[]
        def opener(req,**kwargs):requests.append(req);return self.response()
        p=GeminiProvider(key="AIza_fixture_only",free_plan_confirmed=True,opener=opener)
        raw,meta=p.evaluate(official_rubric(),core.conversation())
        self.assertEqual(len(validate_decisions(raw,official_rubric(),core.conversation())),8)
        self.assertNotIn("AIza",requests[0].full_url)
        self.assertEqual(requests[0].get_header("X-goog-api-key"),"AIza_fixture_only")
        payload=json.loads(requests[0].data)
        self.assertEqual(payload["generationConfig"]["responseFormat"]["text"]["mimeType"],"APPLICATION_JSON")
        self.assertNotIn("tools",payload);self.assertEqual(meta["output_tokens"],40)
        self.assertEqual(meta["visible_output_tokens"],30)

    def test_invalid_reference_is_repaired_once_and_kept(self):
        calls=[];bad=core.ReferenceContractTests().refs();bad["criteria"][0]["evidence_turns"]=["t9999"]
        def opener(req,**kwargs):calls.append(1);return self.response(bad if len(calls)==1 else None)
        p=GeminiProvider(key="AIza_fixture_only",free_plan_confirmed=True,opener=opener)
        _,meta=p.evaluate(official_rubric(),core.conversation())
        self.assertEqual(len(calls),2);self.assertEqual(len(meta["rejected_responses"]),1);self.assertEqual(meta["input_tokens"],200)

    def test_truncated_or_blocked_generation_never_succeeds(self):
        for finish,empty in (("MAX_TOKENS",False),("SAFETY",False),("STOP",True)):
            with self.subTest(finish=finish,empty=empty):
                p=GeminiProvider(key="AIza_fixture_only",free_plan_confirmed=True,opener=lambda *a,**k:self.response(finish=finish,empty=empty))
                with self.assertRaises(ProviderError):p.evaluate(official_rubric(),core.conversation())

    def test_public_config_never_exposes_either_key(self):
        base=Path('.local/test-runs');base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            app=Application(data_dir=folder)
            try:
                app.save_config({'key':'gsk_fixture','gemini_key':'AIza'+'x'*35,'gemini_free_plan_confirmed':True})
                app.config['future_secret']='fixture-secret'
                public=json.dumps(app.public_config())
                for secret in ('gsk_fixture','AIza'+'x'*35,'fixture-secret'):self.assertNotIn(secret,public)
                self.assertTrue(app.public_config()['gemini_key_present'])
            finally:app.store.close()

    def test_daily_quota_reads_retry_info_and_never_retries(self):
        body={'error':{'details':[{'@type':'type.googleapis.com/google.rpc.QuotaFailure','violations':[{'quotaId':'GenerateRequestsPerDayPerProjectPerModel-FreeTier','quotaValue':'20'}]},
                                  {'@type':'type.googleapis.com/google.rpc.RetryInfo','retryDelay':'33100s'}]}}
        calls=[]
        def opener(req,**kwargs):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url,429,'quota',{},io.BytesIO(json.dumps(body).encode()))
        p=GeminiProvider(key='fixture-only-key',free_plan_confirmed=True,opener=opener)
        with self.assertRaises(ProviderError) as caught:p.evaluate(official_rubric(),core.conversation())
        self.assertEqual(len(calls),1);self.assertEqual(caught.exception.retry_after,33100)
        self.assertTrue(caught.exception.metadata['daily_quota'])


class BenchmarkDenominatorTests(unittest.TestCase):
    def test_errors_and_missing_cases_stay_in_expected_denominator(self):
        protocol={'cases':[{'conversation_id':str(i),'family':i//2,'split':'holdout','targets':{'contact':'YES'}} for i in range(4)]}
        attempts=[{'conversation_id':'0','attempt':1,'status':'error','error_code':'quota','latency_ms':1},
                  {'conversation_id':'0','attempt':2,'status':'ok','latency_ms':1000,'target_checks':{'contact':True},'criteria':[{'criterion_id':'contact','answer':'YES'}]},
                  {'conversation_id':'1','attempt':1,'status':'ok','latency_ms':2000,'target_checks':{'contact':False},'criteria':[{'criterion_id':'contact','answer':'NO'}]},
                  {'conversation_id':'2','attempt':1,'status':'error','error_code':'quota','latency_ms':1}]
        report=summarize(protocol,{'split':'holdout'},attempts)
        self.assertEqual((report['expected_cases'],report['valid_cases'],report['error_cases'],report['not_run_cases']),(4,2,1,1))
        self.assertEqual((report['matched_targets'],report['evaluated_targets'],report['expected_targets']),(1,2,4))
        self.assertEqual(report['first_attempt_valid_cases'],1)
        self.assertEqual((report['complete_families'],report['all_targets_matched_families']),(1,0))

    def test_known_partial_usage_survives_failed_evaluation(self):
        protocol={'cases':[{'conversation_id':'partial','family':'one','split':'development','targets':{'contact':'YES'}}]}
        attempts=[{'conversation_id':'partial','attempt':1,'status':'error','error_code':'quota','latency_ms':3,
                   'provenance':{'attempts':2,'attempt_trace':[{'status':'response','input_tokens':100,'output_tokens':30},{'status':'http_error','http_status':429}]}}]
        report=summarize(protocol,{'split':'development'},attempts)
        self.assertEqual(report['known_tokens_all_attempts'],130)
        self.assertEqual(report['valid_cases'],0);self.assertEqual(report['attempts_with_incomplete_usage'],1)
