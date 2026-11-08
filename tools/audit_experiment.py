"""Execute a frozen synthetic audit protocol, preserving all outcomes privately.

The protocol is not human Ground Truth and is never a competition submission.
Use --variant baseline to import the saved, unmodified pre-audit package.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=["baseline", "improved"], required=True)
    args = parser.parse_args()
    package = ROOT/".local/audit-v2/baseline" if args.variant == "baseline" else ROOT
    sys.path.insert(0, str(package))
    from cqa.domain import normalize_conversation, validate_decisions, aggregate
    from cqa.providers import ChatProvider
    from cqa.rubric import official_rubric
    from cqa.store import source_hash
    from cqa.prompts import PROMPT_VERSION, prompt_hash
    protocol = json.loads((ROOT/"evidence/audit-v2/protocol.json").read_text(encoding="utf-8"))
    config = json.loads((ROOT/".local/provider.json").read_text(encoding="utf-8"))
    if not config.get("free_plan_confirmed"):
        raise SystemExit("Free Plan required")
    provider = ChatProvider("groq", protocol["model"], key=config["key"], free_plan_confirmed=True)
    output = ROOT/".local/audit-v2/live"/args.variant
    output.mkdir(parents=True, exist_ok=True)
    rubric = official_rubric()
    code_hash = source_hash()  # Snapshot once: modules are loaded once per process.
    for case in protocol["cases"]:
        target = output/(case["conversation_id"]+".json")
        if target.exists():
            print(json.dumps({"variant": args.variant, "id": case["conversation_id"], "status": "already_recorded"}), flush=True)
            continue  # Failures are also retained, never silently retried/cherry-picked.
        conv = normalize_conversation({k: v for k,v in case.items() if k not in ("targets", "family", "purpose")})
        started = time.perf_counter()
        record = {"conversation_id": conv["conversation_id"], "family": case["family"], "variant": args.variant,
                  "created_at": datetime.now(timezone.utc).isoformat(), "transcript_hash": conv["hash"],
                  "provenance": {"provider": "groq", "model": protocol["model"], "code_hash": code_hash, "prompt_version": PROMPT_VERSION,
                                 "prompt_hash": prompt_hash(), "rubric_hash": rubric["hash"], "is_mock": False},
                  "label_source": "SYNTHETIC_TARGETS_NOT_HUMAN_GROUND_TRUTH", "targets": case["targets"]}
        try:
            raw, metadata = provider.evaluate(rubric, conv)
            record["raw_response"] = raw
            record["provenance"].update(metadata)
            checked = validate_decisions(raw, rubric, conv)
            record.update(status="ok", criteria=checked, original_summary=aggregate(rubric, checked))
            values = {c["criterion_id"]: c["answer"] for c in checked}
            record["target_checks"] = {k: values.get(k)==v for k,v in case["targets"].items()}
        except Exception as exc:
            record.update(status="error", error=str(exc), error_type=type(exc).__name__)
            record["provenance"].update(getattr(exc,"metadata",{}))
            if getattr(exc,"raw_response",None) is not None: record["raw_response"] = exc.raw_response
        record["latency_ms"] = round((time.perf_counter()-started)*1000,2)
        target.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"variant":args.variant,"id":record["conversation_id"],"status":record["status"],"latency_ms":record["latency_ms"],
                          "target_checks":record.get("target_checks"),"error":record.get("error")},ensure_ascii=False),flush=True)
    return 0


if __name__ == "__main__": raise SystemExit(main())
