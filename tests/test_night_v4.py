"""Night release regressions. Fake provider fixtures here never establish AI quality."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cqa.configuration import dotenv_config, environment_config, ENV_FIELDS
from cqa.batch import run_batch
from cqa.domain import normalize_conversation, ValidationError
from cqa.providers import MockProvider, ProviderError
from cqa.server import Application
from test_core import conversation


class FixtureProvider:
    model='requested-alias'
    def __init__(self, error=None):self.calls=0;self.error=error
    def evaluate(self,rubric,conv,mode='evidence'):
        self.calls+=1
        if self.error:raise self.error
        raw,meta=MockProvider().evaluate(rubric,conv,mode)
        meta.update(provider='gemini',model='returned-version',requested_model=self.model,is_mock=False)
        return raw,meta


class NightBatchTests(unittest.TestCase):
    def setUp(self):
        parent=Path('.local/test-runs');parent.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.root=Path(self.tmp.name)
        self.app=Application(data_dir=self.root)
        self.records=[normalize_conversation({**conversation(),'conversation_id':f'batch-fixture-{i}'}) for i in range(3)]
        self.app.store.import_conversations(self.records);self.target=self.root/'results.json'
    def tearDown(self):self.app.store.close();self.tmp.cleanup()
    def run_fixture(self,p,**kwargs):return run_batch(self.app,self.records,p,self.target,kind='gemini',emit=lambda _:None,**kwargs)

    def test_returned_alias_does_not_recompute_successes(self):
        first=FixtureProvider();self.assertEqual(self.run_fixture(first),0);self.assertEqual(first.calls,3)
        second=FixtureProvider(error=AssertionError('must not call fixture twice'))
        self.assertEqual(self.run_fixture(second,resume=True),0);self.assertEqual(second.calls,0)
        self.assertEqual(len(json.loads(self.target.read_text(encoding='utf-8'))),3)

    def test_interruption_preserves_future_cached_successes(self):
        self.run_fixture(FixtureProvider());saved=json.loads(self.target.read_text(encoding='utf-8'))
        saved[0]={'conversation_id':'batch-fixture-0','status':'error','repeat':0}
        self.target.write_text(json.dumps(saved),encoding='utf-8')
        with self.assertRaises(KeyboardInterrupt):self.run_fixture(FixtureProvider(KeyboardInterrupt()),resume=True)
        remaining=json.loads(self.target.read_text(encoding='utf-8'))
        self.assertEqual(remaining,saved[1:])
        self.assertEqual(json.loads(self.target.with_suffix('.json.plan.json').read_text())['status'],'interrupted')
        recovered=FixtureProvider();self.assertEqual(self.run_fixture(recovered,resume=True),0);self.assertEqual(recovered.calls,1)

    def test_daily_quota_stops_and_keeps_full_plan_and_error_metadata(self):
        p=FixtureProvider(ProviderError('quota fixture',code='quota',retry_after=999,metadata={'daily_quota':True,'attempt_trace':[{'status':'http_error','http_status':429}]}))
        self.assertEqual(self.run_fixture(p),2);self.assertEqual(p.calls,1)
        plan=json.loads(self.target.with_suffix('.json.plan.json').read_text());self.assertEqual(plan['total'],3);self.assertEqual(plan['completed'],1)
        row=json.loads(self.target.read_text())[0];self.assertTrue(row['provenance']['daily_quota']);self.assertEqual(row['error_code'],'quota')
        self.assertEqual(len(self.target.with_suffix('.json.attempts.jsonl').read_text().splitlines()),1)

    def test_parameter_change_is_explicit_and_does_not_replace_old_file(self):
        self.run_fixture(FixtureProvider());before=self.target.read_bytes();p=FixtureProvider();p.model='different-model'
        with self.assertRaises(ValidationError):self.run_fixture(p,resume=True)
        self.assertEqual(p.calls,0);self.assertEqual(self.target.read_bytes(),before)

    def test_malformed_resume_is_rejected_without_replacing_file(self):
        self.target.write_text('[null]',encoding='utf-8');before=self.target.read_bytes()
        with self.assertRaises(ValidationError):self.run_fixture(FixtureProvider(),resume=True)
        self.assertEqual(self.target.read_bytes(),before)

    def test_rejected_local_mode_is_not_offered_and_does_not_probe_runtime(self):
        with patch.object(self.app,'provider',side_effect=AssertionError('must not probe')):
            options=self.app.provider_options()
        self.assertEqual([p['name'] for p in options['options']],['groq','gemini'])
        self.assertFalse(options['local_available'])

    def test_cloud_credential_is_never_forwarded_to_local_adapter(self):
        self.app.config['key']='private-cloud-fixture'
        self.assertEqual(self.app.provider('llamacpp','fixture').key,'')
        (self.root/'local-server.key').write_text('local-fixture',encoding='utf-8')
        self.assertEqual(self.app.provider('llamacpp','fixture').key,'local-fixture')


class DotenvTests(unittest.TestCase):
    def test_quotes_comments_and_no_shell_expansion(self):
        with tempfile.TemporaryDirectory(dir='.local/test-runs') as tmp:
            p=Path(tmp)/'.env';p.write_text('export GEMINI_API_KEY="fixture-$(never-execute)"\nCQA_MODEL=gemini-3.5-flash # note\nCQA_GEMINI_FREE_TIER_CONFIRMED=false\nIGNORED=anything\n',encoding='utf-8')
            values=dotenv_config(p);self.assertEqual(values['gemini_key'],'fixture-$(never-execute)');self.assertEqual(values['model'],'gemini-3.5-flash');self.assertFalse(values['gemini_free_plan_confirmed'])

    def test_bad_boolean_has_no_secret_value_in_error(self):
        secret='private-fixture-not-a-bool'
        with self.assertRaises(ValidationError) as caught:environment_config({'CQA_GEMINI_FREE_TIER_CONFIRMED':secret})
        self.assertNotIn(secret,str(caught.exception))

    def test_saved_choices_override_dotenv_and_explicit_process_overrides_saved(self):
        with tempfile.TemporaryDirectory(dir='.local/test-runs') as tmp,patch.dict(os.environ,{k:'' for k in ENV_FIELDS}):
            root=Path(tmp);(root/'.env').write_text('CQA_PROVIDER=gemini\nCQA_MODEL=dotenv-model\n',encoding='utf-8')
            app=Application(data_dir=root)
            self.assertEqual(app.config['model'],'dotenv-model');app.save_config({'model':'saved-model'});app.store.close()
            app=Application(data_dir=root);self.assertEqual(app.config['model'],'saved-model');app.store.close()
            with patch.dict(os.environ,{'CQA_MODEL':'process-model'}):
                app=Application(data_dir=root);self.assertEqual(app.config['model'],'process-model');app.store.close()
