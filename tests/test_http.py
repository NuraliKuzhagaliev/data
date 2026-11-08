import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from pathlib import Path

from cqa.server import Application, create_server
from cqa.providers import ProviderError
from test_core import conversation


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(__file__).resolve().parent.parent / ".local/test-runs"
        base.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=base)
        cls.app = Application(data_dir=cls.tmp.name)
        cls.server = create_server(cls.app, port=0)
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.app.store.close(); cls.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        h = {"Content-Type": "application/json"}
        if data is not None: h["X-CQA-Token"] = self.app.csrf
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=json.dumps(data).encode() if data is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=3) as r: return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e: return e.code, e.read(), e.headers

    def test_health(self):
        status, body, _ = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["code_hash"], self.app.store.code_hash)
    def test_no_key_in_config(self):
        payload = json.loads(self.request("/api/config")[1]); self.assertNotIn("key", payload); self.assertIn("key_present", payload)
    def test_csrf(self): self.assertEqual(self.request("/api/import", {}, {"X-CQA-Token": "wrong"})[0], 403)
    def test_cross_origin(self): self.assertEqual(self.request("/api/import", {}, {"Origin": "https://evil.test"})[0], 403)
    def test_host(self): self.assertEqual(self.request("/api/config", headers={"Host": "evil.test"})[0], 403)
    def test_static_no_traversal(self): self.assertEqual(self.request("/../.local/provider.json")[0], 404)
    def test_security_headers(self):
        _, html, h = self.request("/"); self.assertIn(b"cqa-token", html); self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
    def test_invalid_import(self): self.assertEqual(self.request("/api/import", {"transcript": []})[0], 400)
    def test_empty_latest_job_returns_json_null(self):
        with patch.object(self.app, "latest_job", return_value=None):
            status, body, headers = self.request("/api/latest-job")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body))
        self.assertIn("application/json", headers["Content-Type"])
    def test_latest_job_survives_restart_without_fake_completion(self):
        with tempfile.TemporaryDirectory(dir=Path(self.tmp.name)) as folder:
            first = Application(data_dir=folder)
            self.assertIsNone(first.latest_job())
            job = {"id":"job-" + "a"*32,"created_at":"2026-01-01T00:00:00+00:00","status":"running",
                   "completed":1,"total":2,"outcomes":[],"is_mock":False}
            first._save_job(job); first.jobs[job["id"]] = job
            self.assertEqual(first.latest_job()["status"], "running")
            first.store.close()
            restarted = Application(data_dir=folder)
            try:
                restored = restarted.latest_job()
                self.assertEqual(restored["id"], job["id"])
                self.assertEqual(restored["status"], "interrupted")
                self.assertEqual(restored["completed"], 1)
            finally: restarted.store.close()
        self.assertEqual(self.request("/api/latest-job")[0], 200)
    def test_new_data_full_mock_flow(self):
        status, payload, _ = self.request("/api/import", conversation()); self.assertEqual(status, 201); cid = json.loads(payload)["ids"][0]
        r = self.app.store.rubrics()[0]
        status, payload, _ = self.request("/api/evaluate", {"conversation_ids": [cid], "rubric_id": r["id"], "provider": "mock"}); self.assertEqual(status, 202)
        job_id = json.loads(payload)["id"]
        for _ in range(100):
            job = json.loads(self.request("/api/jobs/" + job_id)[1])
            if job["status"] == "completed": break
            time.sleep(.01)
        self.assertEqual(job["outcomes"][0]["status"], "ok")
        eid = job["outcomes"][0]["evaluation_id"]
        self.assertEqual(self.request(f"/api/evaluations/{eid}/export?format=csv")[0], 200)
        self.assertEqual(self.request(f"/api/jobs/{job_id}/submission?dataset=prod&team=Test")[0], 400)
    def test_failure_job_no_fake_evaluation(self):
        raw = conversation(); raw["conversation_id"] = "failure-input"; cid = json.loads(self.request("/api/import", raw)[1])["ids"][0]
        old = self.app.provider
        class Fail:
            def evaluate(self, *a, **k): raise ProviderError("Synthetic provider outage")
        self.app.provider = lambda *a, **k: Fail()
        before = len(self.app.store.evaluations())
        try:
            status, payload, _ = self.request("/api/evaluate", {"conversation_ids": [cid], "rubric_id": self.app.store.rubrics()[0]["id"], "provider": "groq"}); self.assertEqual(status, 202)
            job_id = json.loads(payload)["id"]
            for _ in range(100):
                job = json.loads(self.request("/api/jobs/" + job_id)[1])
                if job["status"] == "completed": break
                time.sleep(.01)
            self.assertEqual(job["outcomes"][0]["status"], "error"); self.assertEqual(len(self.app.store.evaluations()), before)
        finally: self.app.provider = old

    def wait_for_job(self, app, key):
        for _ in range(200):
            job = app.job(key)
            if job["status"] != "running": return job
            time.sleep(.005)
        self.fail("Job did not finish")

    def test_resume_after_restart_preserves_success_and_retries_only_missing(self):
        from cqa.domain import normalize_conversation
        from cqa.providers import MockProvider
        with tempfile.TemporaryDirectory(dir=Path(self.tmp.name)) as folder:
            app = Application(data_dir=folder)
            inputs = [normalize_conversation({**conversation(), "conversation_id": f"resume-{i}"}) for i in range(3)]
            app.store.import_conversations(inputs); r = app.store.rubrics()[0]
            raw, meta = MockProvider().evaluate(r, inputs[0]); meta["is_mock"] = False; meta["model"] = "transport-fixture-only"
            saved = app.store.save_evaluation(inputs[0], r, raw, meta)
            key = "job-"+"b"*32
            parent = {"id": key, "status": "running", "created_at": "2026-01-01", "completed": 2, "total": 3,
                      "conversation_ids": [c["id"] for c in inputs], "provider": "groq", "model": "fixture", "rubric_id": r["id"], "is_mock": False,
                      "outcomes": [{"internal_id": inputs[0]["id"], "conversation_id": inputs[0]["conversation_id"], "status": "ok", "evaluation_id": saved["id"]},
                                   {"internal_id": inputs[1]["id"], "conversation_id": inputs[1]["conversation_id"], "status": "error", "error": "fixture outage"}]}
            app._save_job(parent); original_file = (Path(folder)/"jobs"/(key+".json")).read_bytes(); app.store.close()
            app = Application(data_dir=folder); calls = []
            class RecoveryFixture:
                def evaluate(self, rubric, conv, mode):
                    calls.append(conv["id"])
                    raw, meta = MockProvider().evaluate(rubric, conv, mode)
                    meta["is_mock"] = False; meta["model"] = "transport-fixture-only"
                    return raw, meta
            try:
                with patch.object(app, "provider", return_value=RecoveryFixture()):
                    child = app.resume_job(key, {"provider": "groq", "model": "qwen/qwen3.8-27b"})
                    child = self.wait_for_job(app, child["id"])
                self.assertEqual(calls, [c["id"] for c in inputs[1:]])
                self.assertEqual(child["completed"], 3); self.assertEqual(child["preserved_successes"], 1)
                self.assertEqual(child["outcomes"][0]["evaluation_id"], saved["id"])
                self.assertEqual(child["rubric_id"], r["id"]); self.assertEqual(child["parent_job_id"], key)
                self.assertEqual(original_file, (Path(folder)/"jobs"/(key+".json")).read_bytes())
                self.assertEqual(len(app.store.evaluations()), 3)
                with self.assertRaises(ValueError): app.resume_job(child["id"], {})
            finally: app.store.close()

    def test_job_plan_persisted_before_first_response(self):
        gate = threading.Event(); entered = threading.Event()
        class Waiting:
            def evaluate(self, *a, **k): entered.set(); gate.wait(2); raise ProviderError("fixture outage")
        cid = json.loads(self.request("/api/import", {**conversation(), "conversation_id": "persist-plan"})[1])["ids"][0]
        try:
            with patch.object(self.app, "provider", return_value=Waiting()):
                _, payload, _ = self.request("/api/evaluate", {"conversation_ids": [cid], "rubric_id": self.app.store.rubrics()[0]["id"], "provider": "groq"})
                key = json.loads(payload)["id"]; self.assertTrue(entered.wait(1))
                plan = json.loads((self.app.data_dir/"jobs"/(key+".json")).read_text(encoding="utf-8"))
                self.assertEqual(plan["conversation_ids"], [cid]); self.assertEqual(plan["completed"], 0)
                self.assertEqual(self.request(f"/api/jobs/{key}/resume", {})[0], 400)
        finally: gate.set()
        self.wait_for_job(self.app, key)
        failures = self.app.store._list("failed_attempts")
        self.assertTrue(any(r["conversation_id"] == "persist-plan" for r in failures))

    def test_resume_unknown_job_and_cross_origin_rejected(self):
        self.assertEqual(self.request("/api/jobs/job-"+"f"*32+"/resume", {})[0], 404)
        self.assertEqual(self.request("/api/jobs/job-"+"f"*32+"/resume", {}, {"Origin": "https://evil.test"})[0], 403)

    def test_real_redirect_does_not_reach_destination(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from cqa.providers import safe_open
        landed = []
        class RedirectFixture(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302); self.send_header("Location", "/landing"); self.end_headers()
            def do_GET(self):
                landed.append(self.headers.get("Authorization"))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/redirect", data=b"synthetic", headers={"Authorization":"Bearer fixture-only"})
        try:
            with urllib.request.urlopen(req, timeout=2) as response: response.read()
            self.assertEqual(landed, ["Bearer fixture-only"])  # Reproduces the old default behavior.
            landed.clear()
            with self.assertRaises(ProviderError): safe_open(req, timeout=2)
            self.assertEqual(landed, [])
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_recheck_preserves_original_and_original_rubric(self):
        from cqa.providers import MockProvider
        from cqa.domain import records_from_payload
        raw={**conversation(),"conversation_id":"recheck-source","source":"synthetic"}
        conv=records_from_payload(raw)[0];self.app.store.import_conversations([conv]);rubric=self.app.store.rubrics()[0]
        output,meta=MockProvider().evaluate(rubric,conv);meta.update(is_mock=False,model="TRANSPORT_FIXTURE_ONLY")
        original=self.app.store.save_evaluation(conv,rubric,output,meta);before=self.app.store._get('evaluations',original['id']);calls=[]
        class RecheckFixture:
            def evaluate(self,received_rubric,received_conv,mode):
                calls.append((received_rubric,received_conv,mode))
                result,metadata=MockProvider().evaluate(received_rubric,received_conv,mode)
                next(c for c in result['criteria'] if c['criterion_id']=='contact')['answer']='NO'
                metadata.update(is_mock=False,model="TRANSPORT_FIXTURE_ONLY")
                return result,metadata
        with patch.object(self.app,'provider',return_value=RecheckFixture()):
            status,body,_=self.request(f"/api/evaluations/{original['id']}/recheck",{'provider':'groq','model':'qwen/qwen3.8-27b'})
            self.assertEqual(status,202);job=self.wait_for_job(self.app,json.loads(body)['id'])
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0]['hash'],rubric['hash'])
        self.assertNotIn('criteria',calls[0][1]);self.assertEqual(calls[0][1]['hash'],conv['hash'])
        self.assertEqual(self.app.store._get('evaluations',original['id']),before)
        repeated=self.app.store.evaluation(job['outcomes'][0]['evaluation_id'])
        self.assertEqual(repeated['comparison']['source_evaluation_id'],original['id'])
        self.assertEqual(repeated['comparison']['changed_criteria'],1)
        self.assertTrue(any(c['needs_review'] for c in repeated['quality']['items']))

    def test_mock_cannot_be_presented_as_independent_real_recheck(self):
        from cqa.providers import MockProvider
        from cqa.domain import records_from_payload
        conv=records_from_payload({**conversation(),'conversation_id':'mock-recheck-reject'})[0]
        self.app.store.import_conversations([conv]);rubric=self.app.store.rubrics()[0]
        output,meta=MockProvider().evaluate(rubric,conv)
        evaluation=self.app.store.save_evaluation(conv,rubric,output,meta)
        self.assertEqual(self.request(f"/api/evaluations/{evaluation['id']}/recheck",{})[0],400)

    def test_long_quota_stops_batch_and_preserves_unprocessed_plan(self):
        ids=[json.loads(self.request('/api/import',{**conversation(),'conversation_id':f'quota-stop-{i}'})[1])['ids'][0] for i in range(3)]
        calls=[]
        class QuotaFixture:
            def evaluate(self,*args,**kwargs):
                calls.append(1)
                raise ProviderError('fixture daily quota',code='quota',retry_after=33100,metadata={'daily_quota':True})
        with patch.object(self.app,'provider',return_value=QuotaFixture()):
            _,body,_=self.request('/api/evaluate',{'conversation_ids':ids,'rubric_id':self.app.store.rubrics()[0]['id'],'provider':'gemini'})
            job=self.wait_for_job(self.app,json.loads(body)['id'])
        self.assertEqual(len(calls),1);self.assertEqual(job['status'],'interrupted')
        self.assertEqual((job['completed'],job['total']),(1,3));self.assertEqual(job['conversation_ids'],ids)
        self.assertTrue(job['outcomes'][0]['daily_quota']);self.assertIn('Лимит',job['stop_reason'])


if __name__ == "__main__": unittest.main()
