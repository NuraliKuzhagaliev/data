"""Matched deterministic audit reproductions. Injected transport, no live AI."""
import argparse
import io
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=("baseline", "improved"), required=True)
    variant = parser.parse_args().variant
    sys.path.insert(0, str(ROOT/".local/audit-v2/baseline" if variant=="baseline" else ROOT))
    from cqa.domain import normalize_conversation, validate_decisions, ValidationError
    from cqa.metrics import diagnostics
    from cqa.providers import ChatProvider, MockProvider
    from cqa.rubric import official_rubric
    from cqa.server import Application
    from cqa.store import source_hash
    from cqa.prompts import PROMPT_VERSION, prompt_hash
    conv = normalize_conversation({"conversation_id": "deterministic-audit-fixture", "source": "synthetic", "transcript": [
        {"speaker":"client","text":"Где общая информация?"}, {"speaker":"operator","text":"Здравствуйте, я Лена. Информация в разделе «Документы»."},
        {"speaker":"operator","text":"Информация в «Документах». Если нужна помощь, обращайтесь. До свидания!"}]})
    rubric = official_rubric()
    raw, _ = MockProvider().evaluate(rubric, conv)
    for c in raw["criteria"]:
        c["evidence_turns"] = [e["turn_id"] for e in c.pop("evidence")]
    calls = []
    def fixture(request, **kwargs):
        calls.append(1)
        value = json.loads(json.dumps(raw))
        if len(calls)==1: next(c for c in value["criteria"] if c["criterion_id"]=="consultation")["answer"]="7"
        class Response(io.BytesIO): headers={}
        return Response(json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps(value)}}],"usage":{"prompt_tokens":100,"completion_tokens":50}}).encode())
    provider = ChatProvider("groq","qwen/qwen3.8-27b",key="gsk_transport_fixture",free_plan_confirmed=True,opener=fixture)
    try:
        result, _ = provider.evaluate(rubric,conv); validate_decisions(result,rubric,conv); repaired=True
    except (ValidationError, RuntimeError): repaired=False
    row=lambda cid,val=10: {"conversation_id":cid,"criteria":[{"criterion_id":"dialogue","value":val}]}
    label={"results":[row("a"),row("b")]}
    criterion=lambda preds: next(c for c in diagnostics(preds,label)["runs"][0]["per_criterion"] if c["criterion_id"]=="dialogue")
    missing=criterion([row("a")])["exact_match"]
    repeat=criterion([row("a")]*20+[row("b",0)])["exact_match"]
    try: criterion([row("a",999)]); accepts_bad_scale=True
    except ValidationError: accepts_bad_scale=False
    base=ROOT/".local/test-runs"; base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as folder:
        app=Application(data_dir=folder); app.store.import_conversations([conv]); gate=threading.Event(); entered=threading.Event()
        class Waiting:
            def evaluate(self,*a,**k): entered.set();gate.wait(3);return MockProvider().evaluate(rubric,conv)
        app.provider=lambda *a,**k:Waiting()
        job=app.start_job({"conversation_ids":[conv["id"]],"rubric_id":rubric["id"],"provider":"mock"})
        entered.wait(1)
        persisted=(Path(folder)/"jobs"/(job["id"]+".json")).exists()
        gate.set()
        for _ in range(100):
            if app.job(job["id"])["status"]!="running":break
            time.sleep(.01)
        app.store.close()
    report={"scope":"DETERMINISTIC_FIXTURES_NOT_MODEL_ACCURACY","variant":variant,
            "reproduced_at":datetime.now(timezone.utc).isoformat(),"code_hash":source_hash(),"prompt_version":PROMPT_VERSION,
            "prompt_hash":prompt_hash(),"rubric_hash":rubric["hash"],"transcript_hash":conv["hash"],
            "missing_one_of_two_exact":missing,"twenty_easy_repeats_one_difficult_exact":repeat,
            "invalid_scale_accepted":accepts_bad_scale,"validator_error_recovered":repaired,"fixture_model_calls":len(calls),
            "job_plan_persisted_before_first_response":persisted,"resume_supported":hasattr(Application,"resume_job")}
    (ROOT/"evidence/audit-v2"/("matched-reproduction-"+variant+".json")).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report))


if __name__=="__main__": main()
