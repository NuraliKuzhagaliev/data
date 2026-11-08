"""Compare complete runs on the same provider, model and frozen targets."""
import json
import statistics
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def load_run(name):
    directory=ROOT/'.local/quality-v3/runs'/name
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    rows={}
    for path in sorted(directory.glob('*-attempt-*.json')):
        row=json.loads(path.read_text(encoding='utf-8'));rows[row['conversation_id']]=row
    return manifest,rows


def main():
    before,old=load_run('baseline-dev')
    after,new=load_run('candidate-qwen-dev-restored')
    for key in ('protocol_hash','provider','model','mode','split'):
        assert before[key]==after[key],f'Non-comparable {key}'
    protocol=json.loads((ROOT/'evidence/quality-v3/protocol.json').read_text(encoding='utf-8'))
    cases=[c for c in protocol['cases'] if c['split']=='development']
    assert len(old)==len(new)==len(cases)==20,'Both full20-case runs required'
    changes=[];families={};old_exact=new_exact=targets=0
    for case in cases:
        cid=case['conversation_id'];a,b=old[cid],new[cid]
        assert a['status']==b['status']=='ok',f'API failure at {cid}'
        assert a['transcript_hash']==b['transcript_hash'],f'Source mismatch {cid}'
        assert a['provenance']['rubric_hash']==b['provenance']['rubric_hash'],f'Rubric mismatch {cid}'
        assert a['provenance']['model']==b['provenance']['model'],f'Returned model mismatch {cid}'
        assert a['targets']==b['targets']==case['targets'],f'Target mismatch {cid}'
        original={r['criterion_id']:r['answer'] for r in a['criteria']}
        changed={r['criterion_id']:r['answer'] for r in b['criteria']}
        family=families.setdefault(case['family'],{'targets':0,'before_matched':0,'after_matched':0})
        for criterion,expected in case['targets'].items():
            x=original[criterion]==expected;y=changed[criterion]==expected
            old_exact+=x;new_exact+=y;targets+=1;family['targets']+=1
            family['before_matched']+=x;family['after_matched']+=y
            if original[criterion]!=changed[criterion]:
                changes.append({'case':cid,'criterion':criterion,'expected':expected,'before':original[criterion],
                                'after':changed[criterion],'effect':'improved' if y and not x else 'regressed' if x and not y else 'changed_without_target_gain'})
    report={'before':before,'after':after,'matched_sources':20,'matched_rubrics':20,'returned_models_identical':True,
            'targets':targets,'before_matched':old_exact,'after_matched':new_exact,'families':len(families),
            'target_improvements':sum(c['effect']=='improved' for c in changes),'target_regressions':sum(c['effect']=='regressed' for c in changes),
            'changes':changes,'by_family':families,
            'success_latency_median_seconds':{'before':round(statistics.median(r['latency_ms'] for r in old.values())/1000,2),
                                              'after':round(statistics.median(r['latency_ms'] for r in new.values())/1000,2)},
            'accuracy_claim_allowed':False,
            'limitations':['Development targets authored by project developer, not human expert Ground Truth.',
                           'Whole pipeline changed, including schema/instructions/token budget; not a pure single-component ablation.',
                           'Related variants remain within10 families;25 targets are not25 independent conversations.',
                           'No claim of statistical significance, generalization or contest ranking.']}
    output=ROOT/'evidence/quality-v3/paired-comparison.json'
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('matched_sources','targets','before_matched','after_matched','target_improvements','target_regressions')},ensure_ascii=False))


if __name__=='__main__':main()
