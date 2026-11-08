"""Actual HTTP boundary tests; provider fixtures never establish AI accuracy."""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from cqa.domain import ValidationError, records_from_payload
from cqa.hosting import create_hosted_server
from cqa.providers import MockProvider
from cqa.rubric import official_rubric
from cqa.store import source_hash
from test_core import conversation


class NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class HostingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = Path('.local/test-runs'); parent.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=parent)
        cls.secret = 'fixture-access-code-not-a-real-password'
        cls.env = patch.dict(os.environ, {'CQA_PROVIDER':'gemini', 'CQA_MODEL':'gemini-3.5-flash',
                                         'GEMINI_API_KEY':'private-gemini-fixture', 'GROQ_API_KEY':'private-groq-fixture',
                                         'CQA_GEMINI_FREE_TIER_CONFIRMED':'true', 'CQA_GROQ_FREE_PLAN_CONFIRMED':'true'})
        cls.env.start()
        cls.server = create_hosted_server(cls.tmp.name, hashlib.sha256(cls.secret.encode()).hexdigest(), origin='https://demo.test')
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.server.registry.close()
        cls.env.stop(); cls.tmp.cleanup()

    def request(self, path, value=None, *, cookie=None, csrf=None, headers=None, form=False):
        h = {'Host':'demo.test', 'Origin':'https://demo.test'}
        if cookie: h['Cookie'] = cookie
        if csrf: h['X-CQA-Token'] = csrf
        if value is not None:
            h['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
            data = urllib.parse.urlencode(value).encode() if form else json.dumps(value).encode()
        else: data = None
        h.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.build_opener(NoFollow(), urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def login(self):
        status, _, headers = self.request('/auth/login', {'access_code':self.secret}, form=True)
        self.assertEqual(status, 303)
        cookie = headers['Set-Cookie'].split(';')[0]
        token = cookie.split('=',1)[1]
        self.addCleanup(self.remove_session, token)
        _, body, _ = self.request('/', cookie=cookie)
        csrf = re.search(r'name="cqa-token" content="([^"]+)"', body.decode()).group(1)
        return cookie, csrf, self.server.registry.get(token)

    def remove_session(self, token):
        with self.server.registry.lock:
            entry = self.server.registry.sessions.pop(token, None)
            if entry: entry['app'].store.close()

    def test_public_health_and_login_never_expose_key_or_transcript(self):
        status, body, _ = self.request('/api/health')
        self.assertEqual(status, 200); self.assertEqual(json.loads(body)['code_hash'], source_hash())
        status, body, _ = self.request('/')
        self.assertIn('Код доступа'.encode(), body)
        for secret in [b'private-gemini-fixture',b'private-groq-fixture']:
            self.assertNotIn(secret,body)
        self.assertEqual(self.request('/api/config')[0],401)
        self.assertEqual(self.request('/api/conversations')[0],401)
        self.assertEqual(self.request('/api/evaluate',{})[0],401)

    def test_origin_and_host_checks_apply_before_login(self):
        self.assertEqual(self.request('/api/health',headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.request('/auth/login',{'access_code':self.secret},form=True,headers={'Origin':'https://evil.test'})[0],403)

    def test_html_login_policy_preserves_origin_and_null_origin_is_still_rejected(self):
        status,_,headers=self.request('/')
        self.assertEqual(status,200);self.assertEqual(headers['Referrer-Policy'],'same-origin')
        self.assertEqual(self.request('/auth/login',{'access_code':self.secret},form=True,headers={'Origin':'null'})[0],403)

    def test_wrong_password_does_not_create_session(self):
        before=len(self.server.registry.sessions)
        status,_,headers=self.request('/auth/login',{'access_code':'wrong'},form=True)
        self.assertEqual(status,303); self.assertEqual(headers['Location'],'/login?error=1')
        self.assertNotIn('Set-Cookie',headers); self.assertEqual(before,len(self.server.registry.sessions))

    def test_cookie_is_secure_http_only_and_forgery_is_rejected(self):
        cookie,csrf,app=self.login()
        self.assertEqual(self.request('/api/config',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/config',cookie='cqa_session=../../guess')[0],401)
        status,_,headers=self.request('/auth/login',{'access_code':self.secret},form=True)
        cookie2=headers['Set-Cookie']; self.addCleanup(self.remove_session,cookie2.split(';')[0].split('=',1)[1])
        for attribute in ['HttpOnly','Secure','SameSite=Lax','Path=/']:
            self.assertIn(attribute,cookie2)

    def test_distinct_logins_isolate_data_and_csrf(self):
        cookie1,csrf1,app1=self.login();cookie2,csrf2,app2=self.login()
        record={**conversation(),'conversation_id':'private-session-input'}
        status,body,_=self.request('/api/import',record,cookie=cookie1,csrf=csrf1)
        self.assertEqual(status,201);cid=json.loads(body)['ids'][0]
        self.assertEqual(self.request('/api/conversations/'+cid,cookie=cookie1)[0],200)
        self.assertEqual(self.request('/api/conversations/'+cid,cookie=cookie2)[0],404)
        self.assertEqual(self.request('/api/import',record,cookie=cookie2,csrf=csrf1)[0],403)
        self.assertNotEqual(csrf1,csrf2);self.assertNotEqual(app1.data_dir,app2.data_dir)

    def test_managed_credentials_cannot_be_replaced_or_written(self):
        cookie,csrf,app=self.login()
        status,body,_=self.request('/api/config',cookie=cookie)
        config=json.loads(body);self.assertTrue(config['hosted']);self.assertTrue(config['credentials_managed'])
        self.assertNotIn(b'private-gemini-fixture',body);self.assertNotIn(b'private-groq-fixture',body)
        self.assertEqual(self.request('/api/config',{'gemini_key':'attacker-fixture'},cookie=cookie,csrf=csrf)[0],400)
        self.assertEqual(self.request('/api/config',{'provider':'groq','model':'openai/gpt-oss-120b'},cookie=cookie,csrf=csrf)[0],200)
        self.assertEqual(app.config['gemini_key'],'private-gemini-fixture')
        self.assertFalse(app.config_path.exists())
        self.assertEqual(self.request('/api/config',{'provider':'gemini','model':'unknown'},cookie=cookie,csrf=csrf)[0],400)

    def test_local_providers_and_private_dev_are_not_hosted(self):
        cookie,csrf,app=self.login()
        self.assertEqual(self.request('/api/models?provider=llamacpp',cookie=cookie)[0],403)
        self.assertEqual(self.request('/api/import-dev',{},cookie=cookie,csrf=csrf)[0],400)
        status,body,_=self.request('/api/models?provider=gemini',cookie=cookie)
        self.assertEqual([m['name'] for m in json.loads(body)['models']],['gemini-3.5-flash'])
        self.assertEqual(self.request('/../.local/provider.json',cookie=cookie)[0],404)

    def test_batch_limit_applies_before_any_provider_call(self):
        cookie,csrf,app=self.login()
        with patch.object(app,'provider',side_effect=AssertionError('no network allowed')):
            status,_,_=self.request('/api/evaluate',{'conversation_ids':['x'+str(i) for i in range(11)]},cookie=cookie,csrf=csrf)
        self.assertEqual(status,400)

    def test_auth_and_upload_size_are_bounded(self):
        self.assertEqual(self.request('/auth/login',{'access_code':'x'*2050},form=True)[0],413)
        cookie,csrf,_=self.login()
        self.assertEqual(self.request('/api/import',{'text':'x'*2_000_000},cookie=cookie,csrf=csrf)[0],413)

    def test_logout_requires_csrf_and_clears_cookie(self):
        cookie,csrf,_=self.login()
        self.assertEqual(self.request('/auth/logout',{},cookie=cookie,csrf='wrong')[0],403)
        status,_,headers=self.request('/auth/logout',{},cookie=cookie,csrf=csrf)
        self.assertEqual(status,303);self.assertIn('Max-Age=0',headers['Set-Cookie'])
        self.assertEqual(self.request('/api/config',cookie=cookie)[0],401)

    def test_startup_refuses_missing_access_hash_and_non_https_public_origin(self):
        with self.assertRaises(ValidationError):create_hosted_server(self.tmp.name,'',origin='https://demo.test')
        with self.assertRaises(ValidationError):create_hosted_server(self.tmp.name,'a'*64,origin='http://public.test')

    def test_budget_and_concurrency_cover_all_workspaces(self):
        _,_,first=self.login();_,_,second=self.login();registry=self.server.registry
        before_limit, before_used = registry.daily_limit, registry.used
        first_id=first.store.conversations()[0]['id'];second_id=second.store.conversations()[0]['id']
        registry.reset_budget();registry.daily_limit=1;registry.used=1
        with patch.object(first,'provider',side_effect=AssertionError('budget must block network')):
            with self.assertRaises(ValidationError):first.start_job({'conversation_ids':[first_id],'rubric_id':official_rubric()['id']})
        registry.daily_limit=before_limit;registry.used=before_used
        started=threading.Event();release=threading.Event()
        class BlockingFixture(MockProvider):
            def evaluate(self,*args,**kwargs):
                started.set();release.wait(3);return super().evaluate(*args,**kwargs)
        with patch.object(first,'provider',return_value=BlockingFixture()):
            job=first.start_job({'conversation_ids':[first_id],'rubric_id':official_rubric()['id'],'provider':'mock'})
            self.assertTrue(started.wait(2))
            try:
                with self.assertRaises(ValidationError):second.start_job({'conversation_ids':[second_id],'rubric_id':official_rubric()['id'],'provider':'mock'})
            finally:release.set()
        for _ in range(100):
            if first.job(job['id'])['status']!='running':break
            time.sleep(.01)
        self.assertEqual(first.job(job['id'])['status'],'completed')


class PublicHostingTests(unittest.TestCase):
    request = HostingTests.request
    remove_session = HostingTests.remove_session

    @classmethod
    def setUpClass(cls):
        parent = Path('.local/test-runs'); parent.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=parent)
        cls.server = create_hosted_server(cls.tmp.name, '', origin='https://demo.test', public_access=True, daily_limit=200)
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.server.registry.close(); cls.tmp.cleanup()

    def open_session(self):
        status, _, headers = self.request('/')
        self.assertEqual(status, 303); self.assertEqual(headers['Location'], '/')
        cookie = headers['Set-Cookie'].split(';')[0]
        self.addCleanup(self.remove_session, cookie.split('=', 1)[1])
        for attribute in ['Secure', 'HttpOnly', 'SameSite=Lax']:
            self.assertIn(attribute, headers['Set-Cookie'])
        status, body, _ = self.request('/', cookie=cookie)
        self.assertEqual(status, 200); self.assertNotIn('Код доступа команды'.encode(), body)
        csrf = re.search(r'name="cqa-token" content="([^"]+)"', body.decode()).group(1)
        return cookie, csrf

    def test_no_code_entry_and_existing_session_is_reused(self):
        cookie, _ = self.open_session(); count = len(self.server.registry.sessions)
        self.assertNotIn('Set-Cookie', self.request('/', cookie=cookie)[2])
        self.assertEqual(self.request('/login', cookie=cookie)[2]['Location'], '/')
        self.assertEqual(len(self.server.registry.sessions), count)
        status, body, _ = self.request('/api/config', cookie=cookie)
        cfg = json.loads(body); self.assertTrue(cfg['public_access']); self.assertTrue(cfg['credentials_managed'])
        self.assertNotIn('key', cfg); self.assertNotIn('gemini_key', cfg)
        self.assertEqual(self.server.registry.daily_limit, 200)

    def test_public_visitors_still_have_isolated_data_and_csrf(self):
        cookie1, csrf1 = self.open_session(); cookie2, csrf2 = self.open_session()
        status, body, _ = self.request('/api/import', conversation(), cookie=cookie1, csrf=csrf1)
        self.assertEqual(status, 201); cid = json.loads(body)['ids'][0]
        self.assertEqual(self.request('/api/conversations/' + cid, cookie=cookie2)[0], 404)
        self.assertEqual(self.request('/api/import', conversation(), cookie=cookie2, csrf=csrf1)[0], 403)
        self.assertNotEqual(csrf1, csrf2)
        self.assertEqual(self.request('/api/config', {'key':'replacement'}, cookie=cookie1, csrf=csrf1)[0], 400)

    def test_public_mode_still_checks_origin_and_capacity(self):
        self.assertEqual(self.request('/', headers={'Origin':'https://evil.test'})[0], 403)
        self.assertEqual(self.request('/api/config')[0], 401)
        self.assertEqual(self.request('/api/health')[0], 200)
        self.server.registry.maximum = 0
        try:
            self.assertEqual(self.request('/')[0], 503)
        finally:
            self.server.registry.maximum = 16
