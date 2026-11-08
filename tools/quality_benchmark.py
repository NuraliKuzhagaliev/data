"""Run frozen synthetic cases with real providers; keep errors and versions.

Do not inspect holdout output until candidate-freeze.json exists. Results are
diagnostic assertions, never human-GT accuracy or an organizer submission.
"""
import argparse
import hashlib
import json
import sys
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run',required=True)
    p.add_argument('--split',choices=('development','holdout'),required=True)
    p.add_argument('--package',choices=('baseline','current'),default='current')
    p.add_argument('--model',default='qwen/qwen3.8-27b')
    p.add_argument('--provider',choices=('groq','gemini'),default='groq')
    p.add_argument('--mode',default='evidence')
    p.add_argument('--pacing-seconds',type=float,default=0)
    p.add_argument('--retry-errors',action='store_true')
    p.add_argument('--case',action='append',help='Explicit diagnostic subset; missing cases remain NOT_RUN in full report')
    args=p.parse_args()
    if not args.run.replace('-','').replace('_','').isalnum():p.error('Simple run name required')
    protocol_path=ROOT/'evidence/quality-v3/protocol.json'
    protocol=json.loads(protocol_path.read_text(encoding='utf-8'))
    protocol_hash=hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    assert protocol_hash==protocol_path.with_suffix('.sha256').read_text(encoding='utf-8')
    if args.split=='holdout' and not (ROOT/'evidence/quality-v3/candidate-freeze.json').exists():
        p.error('Freeze candidate before accessing holdout predictions')
    package=ROOT/'.local/quality-v3/baseline' if args.package=='baseline' else ROOT
    sys.path.insert(0,str(package))
    from cqa.domain import normalize_conversation,validate_decisions,aggregate
    from cqa.rubric import official_rubric
    from cqa.store import source_hash
    from cqa.prompts import prompt_hash,PROMPT_VERSION
    from cqa.providers import ChatProvider,ProviderError
    grading_files=('domain.py','rubric.py','prompts.py','providers.py')+(('gemini.py',) if args.provider=='gemini' else ())
    grading_hash=hashlib.sha256(b''.join(name.encode()+b'\0'+(package/'cqa'/name).read_bytes() for name in grading_files)).hexdigest()
    if args.split=='holdout':
        freeze=json.loads((ROOT/'evidence/quality-v3/candidate-freeze.json').read_text(encoding='utf-8'))
        if freeze['protocol_hash']!=protocol_hash:p.error('Protocol differs from frozen protocol')
        if args.package=='current':
            signature={'provider':args.provider,'model':args.model,'mode':args.mode,'prompt_hash':prompt_hash(args.mode),'grading_code_hash':grading_hash}
            if signature not in [freeze['candidate'],*freeze.get('availability_fallbacks',[])]:p.error('Candidate differs from freeze; do not tune on holdout')
    config=json.loads((ROOT/'.local/provider.json').read_text(encoding='utf-8'))
    if args.provider=='groq':
        if not config.get('free_plan_confirmed'):p.error('Confirmed Free Plan required')
        provider=ChatProvider('groq',args.model,key=config['key'],free_plan_confirmed=True)
    else:
        from cqa.gemini import GeminiProvider
        if not config.get('gemini_free_plan_confirmed'):p.error('Confirmed Gemini Free Tier required')
        provider=GeminiProvider(args.model,key=config.get('gemini_key',''),free_plan_confirmed=True)
    output=ROOT/'.local/quality-v3/runs'/args.run;output.mkdir(parents=True,exist_ok=True)
    code_hash=source_hash();rubric=official_rubric()
    snapshot=output/'source_snapshot'
    if not snapshot.exists():
        for folder in ('cqa','web'):
            for source in (package/folder).glob('*'):
                if source.is_file() and source.suffix in ('.py','.js','.html','.css'):
                    target=snapshot/source.relative_to(package);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    manifest={'run':args.run,'protocol_hash':protocol_hash,'code_hash':code_hash,'prompt_hash':prompt_hash(args.mode),
              'model':args.model,'provider':args.provider,'mode':args.mode,'split':args.split,'label_source':'synthetic','human_ground_truth':False}
    if args.case:manifest['requested_subset']=args.case
    manifest_path=output/'manifest.json'
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding='utf-8'))!=manifest:
        p.error('Run manifest changed; use a new run name')
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    for case in [c for c in protocol['cases'] if c['split']==args.split]:
        if args.case and case['conversation_id'] not in args.case:continue
        previous=list(output.glob(case['conversation_id']+'-attempt-*.json'))
        if previous:
            last=json.loads(sorted(previous)[-1].read_text(encoding='utf-8'))
            if last['status']=='ok' or not args.retry_errors:continue
        attempt=len(previous)+1
        # Explicit allowlist: expected answers, family and test split never reach LLM.
        conv=normalize_conversation({k:case[k] for k in ('conversation_id','source','language','transcript')})
        result={'conversation_id':case['conversation_id'],'family':case['family'],'split':args.split,'status':'error',
                'created_at':datetime.now(timezone.utc).isoformat(),'attempt':attempt,'targets':case['targets'],'transcript_hash':conv['hash'],
                'provenance':{**manifest,'prompt_version':PROMPT_VERSION,'rubric_hash':rubric['hash'],'is_mock':False}}
        started=time.perf_counter()
        try:
            raw,meta=provider.evaluate(rubric,conv,mode=args.mode)
            result['raw_response']=raw;result['provenance'].update(meta)
            checked=validate_decisions(raw,rubric,conv)
            result.update(status='ok',criteria=checked,original_summary=aggregate(rubric,checked))
            values={c['criterion_id']:c['answer'] for c in checked}
            result['target_checks']={k:values.get(k)==v for k,v in case['targets'].items()}
        except Exception as exc:
            result.update(error=str(exc),error_type=type(exc).__name__,error_code=getattr(exc,'code','unexpected'))
            result['retry_after']=getattr(exc,'retry_after',None)
            result['provenance'].update(getattr(exc,'metadata',{}))
            if getattr(exc,'raw_response',None) is not None:result['raw_response']=exc.raw_response
        result['latency_ms']=round((time.perf_counter()-started)*1000,2)
        (output/(case['conversation_id']+f'-attempt-{attempt:03}.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'run':args.run,'id':case['conversation_id'],'status':result['status'],
                          'checks':result.get('target_checks'),'error_code':result.get('error_code'),'latency_ms':result['latency_ms']},ensure_ascii=False),flush=True)
        if result.get('error_code') in ('rate_limit','quota') and ((result.get('retry_after') or 0)>55 or result['provenance'].get('daily_quota')):
            break
        if args.pacing_seconds>0:time.sleep(min(55,args.pacing_seconds))
    return 0


if __name__=='__main__':raise SystemExit(main())
