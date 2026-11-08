"""Publish aggregate evidence only; no transcripts, raw quotes or credentials."""
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from cqa.metrics import percentile


def summarize(path, targets):
    records=json.loads(path.read_text(encoding="utf-8"))
    successes=[r for r in records if r.get("status")!="error"]
    latencies=[r["provenance"]["latency_ms"] for r in successes]
    by_case={}
    target_checks=[]
    for r in successes:
        cid=r["external_conversation_id"]
        values={c["criterion_id"]:c["answer"] for c in r["criteria"]}
        by_case.setdefault(cid,[]).append((values,r["original_summary"]["total"]))
        for criterion, expected in targets.get(cid,{}).items():
            target_checks.append(values.get(criterion)==("N/A" if expected is None else "YES" if expected is True else "NO" if expected is False else str(expected)))
    repeated=[]
    for cid, runs in by_case.items():
        if len(runs)<2: continue
        totals=[r[1] for r in runs if r[1] is not None]
        repeated.append({"conversation_id":cid,"n":len(runs),"total_range":round(max(totals)-min(totals),2) if totals else None,
                         "criteria_with_flips":[c for c in runs[0][0] if len({r[0].get(c) for r in runs})>1]})
    return {"file":str(path.relative_to(ROOT)),"calls_recorded":len(records),"unique_successful_cases":len(by_case),"valid_results":len(successes),"errors":len(records)-len(successes),
            "latency_ms":{"median":statistics.median(latencies) if latencies else None,"p95":percentile(latencies,.95),"n":len(latencies)},
            "input_tokens_known":sum(r["provenance"].get("input_tokens",0) or 0 for r in successes),
            "output_tokens_known":sum(r["provenance"].get("output_tokens",0) or 0 for r in successes),
            "cached_input_tokens_known":sum(r["provenance"].get("cached_input_tokens",0) or 0 for r in successes),
            "authored_target_checks":{"pass":sum(target_checks),"n":len(target_checks),"scope":"Synthetic selected features only, not human or competition accuracy"},
            "repeatability":repeated,
            "failures":[{"conversation_id":r.get("conversation_id"),"error":r.get("error")} for r in records if r.get("status")=="error"],
            "model":sorted({r.get("provenance",{}).get("model","UNKNOWN") for r in records}),
            "prompt_hashes":sorted({r.get("provenance",{}).get("prompt_hash","UNKNOWN") for r in records}),
            "code_hashes":sorted({r.get("provenance",{}).get("code_hash","UNKNOWN") for r in records})}


def main():
    targets=json.loads((ROOT/"evidence/stress-protocol.json").read_text(encoding="utf-8"))["targets"]
    files={"baseline_v3":ROOT/".local/experiments/local-baseline-v3.json", "evidence_v3_two_repeats":ROOT/".local/experiments/local-stress-v3.json"}
    report={"status":"PASS_EXECUTION_ONLY","label_source":"synthetic","competition_accuracy":"NOT_RUN_NO_HUMAN_GROUND_TRUTH","cloud_groq":"LIVE_FREE_PLAN_CALLS_EXECUTED",
            "runs":{name:summarize(path,targets) for name,path in files.items() if path.exists()},
            "limitations":["Six authored scenarios; no statistical accuracy superiority claim","Two repetitions are not independent conversations","Greedy generation and fixed seed did not ensure identical scores","Early v1/v2 failures are retained separately; schema changes prevent a clean accuracy comparison with v3","Local model shows changes in unrelated criteria after a greeting-only edit; expert validation required"]}
    target=ROOT/"evidence/local-model-evaluation.json"
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    cloud_path=ROOT/".local/experiments/groq-evidence-v4.json"
    if cloud_path.exists():
        cloud_targets=json.loads((ROOT/"evidence/stress-protocol-v4.json").read_text(encoding="utf-8"))["targets"]
        cloud_report={"status":"PASS_EXECUTION_ONLY","label_source":"synthetic","competition_accuracy":"NOT_RUN_NO_HUMAN_GROUND_TRUTH",
                      "run":summarize(cloud_path,cloud_targets),
                      "limitations":["Eight authored development cases, selected features only; not a held-out accuracy benchmark", "Prompt v4 differs from local v3: no controlled cloud/local superiority claim", "Latency includes bounded rate-limit waits; cache hits are not guaranteed", "Free Plan confirmed by user, billing not programmatically verified"]}
        (ROOT/"evidence/cloud-model-evaluation.json").write_text(json.dumps(cloud_report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"report":str(target),"runs":{k:{"valid":v["valid_results"],"errors":v["errors"],"median_ms":v["latency_ms"]["median"]} for k,v in report["runs"].items()}},ensure_ascii=False))


if __name__=="__main__":main()
