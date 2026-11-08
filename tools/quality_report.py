"""Summarize all frozen cases, including failed and unrun predictions.

Use latest attempt per case for completion, retaining first-attempt outcomes,
all retries, actual denominators, and the paired-family statistical unit.
"""
import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def summarize(protocol, manifest, attempts):
    cases=[c for c in protocol['cases'] if c['split']==manifest['split']]
    by_id={c['conversation_id']:[] for c in cases}
    for result in attempts:
        if result['conversation_id'] in by_id:by_id[result['conversation_id']].append(result)
    rows=[];latencies=[];tokens=0;unknown_usage=0;http_attempts=0
    for cid,history in by_id.items():
        history.sort(key=lambda r:r['attempt'])
        case=next(c for c in cases if c['conversation_id']==cid)
        row={'id':cid,'family':case['family'],'status':'NOT_RUN','targets':case['targets'],'checks':{},'attempts':len(history)}
        if history:
            last=history[-1];ok=last['status']=='ok'
            row.update(status='PASS' if ok else 'ERROR',checks=last.get('target_checks',{}),first_attempt_status=history[0]['status'],
                       error_code=last.get('error_code'),latency_ms=last['latency_ms'])
            if ok:latencies.append(last['latency_ms']/1000)
            if ok:
                answers={c['criterion_id']:c['answer'] for c in last['criteria']}
                row['mismatches']={k:{'expected':v,'actual':answers.get(k)} for k,v in case['targets'].items() if answers.get(k)!=v}
        rows.append(row)
        for result in history:
            provenance=result.get('provenance',{})
            responses=[t for t in provenance.get('attempt_trace',[]) if t.get('status')=='response']
            values=[t.get(k) for t in responses for k in ('input_tokens','output_tokens')] if responses else [provenance.get('input_tokens'),provenance.get('output_tokens')]
            tokens+=sum(v for v in values if isinstance(v,int))
            if not provenance.get('token_usage_complete',False):unknown_usage+=1
            http_attempts+=provenance.get('attempts',0)
    valid=[r for r in rows if r['status']=='PASS']
    returned_models=Counter(history[-1].get('provenance',{}).get('model',manifest.get('model','UNKNOWN')) for history in by_id.values() if history and history[-1]['status']=='ok')
    matched=sum(sum(r['checks'].values()) for r in valid)
    evaluated=sum(len(r['targets']) for r in valid)
    total=sum(len(c['targets']) for c in cases)
    families={r['family'] for r in rows}
    family_complete=[f for f in families if all(r['status']=='PASS' for r in rows if r['family']==f)]
    family_pass=[f for f in family_complete if all(all(r['checks'].values()) for r in rows if r['family']==f)]
    criterion={}
    for row in rows:
        for cid in row['targets']:
            counter=criterion.setdefault(cid,{'expected':0,'evaluated':0,'matched':0})
            counter['expected']+=1
            if row['status']=='PASS':counter['evaluated']+=1;counter['matched']+=bool(row['checks'].get(cid))
    return {'manifest':manifest,'expected_cases':len(cases),'valid_cases':len(valid),'error_cases':sum(r['status']=='ERROR' for r in rows),
            'not_run_cases':sum(r['status']=='NOT_RUN' for r in rows),'expected_targets':total,'evaluated_targets':evaluated,'matched_targets':matched,
            'first_attempt_valid_cases':sum(r.get('first_attempt_status')=='ok' for r in rows),
            'families':len(families),'complete_families':len(family_complete),'all_targets_matched_families':len(family_pass),
            'latency_success_seconds':{'n':len(latencies),'median':round(statistics.median(latencies),2) if latencies else None,'max':round(max(latencies),2) if latencies else None},
            'known_tokens_all_attempts':tokens,'attempts_with_incomplete_usage':unknown_usage,'http_attempts':http_attempts,
            'requested_model':manifest.get('model'),'returned_model_versions':dict(returned_models),
            'by_criterion':criterion,'cases':rows,'human_ground_truth':False,
            'limitations':['Targets authored by project developer, not independent human Ground Truth.',
                           'Cases within one family are related; family is the comparison unit.',
                           'Matched/evaluated excludes API failures; matched/expected includes missing outputs.',
                           'Known tokens exclude missing provider usage, including some failures.',
                           'Latency on short synthetic cases cannot establish PROD deadline readiness.']}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='evidence/quality-v3/benchmark-report.json')
    args=parser.parse_args();output=(ROOT/args.output).resolve()
    if not output.is_relative_to(ROOT):parser.error('Output must stay in workspace')
    protocol=json.loads((ROOT/'evidence/quality-v3/protocol.json').read_text(encoding='utf-8'))
    reports=[];all_attempts=[]
    for directory in sorted((ROOT/'.local/quality-v3/runs').iterdir()):
        if not (directory/'manifest.json').exists():continue
        manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        attempts=[json.loads(p.read_text(encoding='utf-8')) for p in directory.glob('*-attempt-*.json')]
        all_attempts.extend(attempts)
        reports.append(summarize(protocol,manifest,attempts))
    expected={c['conversation_id'] for c in protocol['cases']}
    attempted={r['conversation_id'] for r in all_attempts}&expected
    valid={r['conversation_id'] for r in all_attempts if r['status']=='ok'}&expected
    inventory={'expected':len(expected),'attempted':len(attempted),'with_at_least_one_valid_output':len(valid),
               'failed_only':sorted(attempted-valid),'never_attempted':sorted(expected-attempted),
               'composite_accuracy':None,
               'scope':'Completeness inventory across baseline and candidate versions/providers; never a single-model accuracy estimate or selection by score.'}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({'protocol':str('evidence/quality-v3/protocol.json'),'inventory':inventory,'runs':reports},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Unique-case inventory:',json.dumps(inventory,ensure_ascii=False))
    for r in reports:
        print(f"{r['manifest']['run']}: {r['valid_cases']}/{r['expected_cases']} valid; {r['matched_targets']}/{r['evaluated_targets']} matched evaluated targets; {r['not_run_cases']} NOT_RUN")


if __name__=='__main__':main()
