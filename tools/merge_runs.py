"""Assemble complete automatic submissions after explicit subset retries.

Later successful whole evaluations replace earlier ones in the declared input
order. Scores never influence selection. Original run files remain untouched.
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cqa.domain import ValidationError, aggregate, digest, records_from_payload, validate_decisions
from cqa.exporting import official_record, submission, validate_submission


def assemble(records, runs):
    sources = {r["conversation_id"]: r for r in records}
    if len(sources) != len(records):
        raise ValidationError("Повтор исходного conversation_id")
    chosen, origins = {}, {}
    for origin, rows in runs:
        seen = set()
        for row in rows:
            cid = row.get("external_conversation_id", row.get("conversation_id"))
            if cid not in sources or cid in seen:
                raise ValidationError(f"{origin}: чужой или повторный ID {cid}")
            seen.add(cid)
            if row.get("status") != "error":
                source = sources[cid]
                if row.get("transcript_hash") != source["hash"]:
                    raise ValidationError(f"{cid}: изменился исходный текст/канал/язык")
                decisions = validate_decisions({"criteria": row["criteria"]}, row["rubric"], source)
                if aggregate(row["rubric"], decisions) != row["original_summary"]:
                    raise ValidationError(f"{cid}: исходный итог не соответствует решениям")
                official_record(row)  # Reject MOCK, synthetic inputs, changed official scales.
            if cid not in chosen or row.get("status") != "error":
                chosen[cid], origins[cid] = row, origin
    if set(chosen) != set(sources):
        raise ValidationError(f"Нет исходов для {len(set(sources)-set(chosen))} записей")
    return [chosen[r["conversation_id"]] for r in records], origins


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description="Собрать официальный пакет из реального UI-запуска и явных повторов ошибок")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--group", choices=("dev", "prod"), required=True)
    parser.add_argument("--team", required=True)
    parser.add_argument("--job", help="Завершённый пакет основного локального приложения")
    parser.add_argument("--runs", nargs="*", default=[], help="Массивы исходов CLI; позже = приоритетнее, только целый успешный ответ")
    parser.add_argument("--output")
    parser.add_argument("--restore-ui", action="store_true", help="Проверенные исходные оценки скопировать в рабочую библиотеку с сохранением ID, времени и provenance")
    args = parser.parse_args()
    records = records_from_payload(json.loads(Path(args.dataset).read_text(encoding="utf-8-sig")))
    if len(records) != (65 if args.group == "dev" else 10):
        raise ValidationError("Неверное число исходных записей набора")
    runs = []
    if args.job:
        from cqa.server import Application
        app = Application()
        job = app.job(args.job)
        if job["status"] != "completed" or job["completed"] != job["total"] or job["is_mock"]:
            raise ValidationError("Основной пакет должен завершиться; MOCK запрещён")
        rows = [app.store.evaluation(r["evaluation_id"]) if r["status"] == "ok" else r for r in job["outcomes"]]
        runs.append((args.job, rows))
        app.store.close()
    for name in args.runs:
        rows = json.loads(Path(name).read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list):
            raise ValidationError(f"{name}: ожидается массив исходов")
        runs.append((name, rows))
    outcomes, origins = assemble(records, runs)
    expected = [r["conversation_id"] for r in records]
    payload = submission(args.team, outcomes, args.group, expected)
    validation = validate_submission(payload, expected, {r["conversation_id"]: len(r["transcript"]) for r in records})
    target = Path(args.output or f"results/{args.group}/results.json")
    atomic_json(ROOT / f".local/experiments/{args.group}-final.json", outcomes)
    atomic_json(target, payload)
    report = {"status": "PASS" if not validation["errors"] else "VALID_WITH_EVALUATION_ERRORS",
              "scope": "Submission integrity, not human accuracy", "team": args.team,
              "policy": "Declared input order; latest successful whole automatic answer. Scores ignored. Human reviews ignored.",
              "validation": validation, "model": payload["model"], "output": str(target),
              "canonical_payload_sha256": digest(payload), "selected_origins": origins}
    if args.restore_ui:
        from cqa.server import Application
        from cqa.store import now
        app = Application()
        app.store.import_conversations(records)
        outcomes_for_job = []
        source_by_id = {r["conversation_id"]: r for r in records}
        for row in outcomes:
            cid = row.get("external_conversation_id", row.get("conversation_id"))
            source = source_by_id[cid]
            if row.get("status") == "error":
                outcomes_for_job.append({"conversation_id":cid,"internal_id":source["id"],"status":"error","error":row["error"]})
                continue
            if row["conversation_id"] != source["id"]:
                raise ValidationError(f"{cid}: другая исходная группа/внутренний ID; перенос отклонён")
            body = {k:v for k,v in row.items() if k not in ("appeals","final_criteria","final_summary","repeat")}
            app.store.save_rubric(row["rubric"])
            with app.store.lock, app.store.db:
                existing = app.store.db.execute("SELECT body FROM evaluations WHERE id=?", (row["id"],)).fetchone()
                if existing and json.loads(existing[0]) != body:
                    raise ValidationError("Конфликт ID сохранённого аудита; исходная запись не перезаписана")
                app.store.db.execute("INSERT OR IGNORE INTO evaluations VALUES (?,?,?)", (row["id"],source["id"],json.dumps(body,ensure_ascii=False)))
            outcomes_for_job.append({"conversation_id":cid,"internal_id":source["id"],"status":"ok","evaluation_id":row["id"]})
        archive_job={"id":"job-"+uuid.uuid4().hex,"status":"completed","created_at":now(),"completed_at":now(),
                     "completed":len(outcomes_for_job),"total":len(outcomes_for_job),"outcomes":outcomes_for_job,
                     "provider":"assembled_original_runs","model":payload["model"]["name"],"rubric_id":None,"is_mock":False,
                     "assembled_from":[name for name,_ in runs],"restored_original_audits":True}
        app._save_job(archive_job)
        app.store.close()
        report["restored_ui_job"] = archive_job["id"]
    atomic_json(ROOT / f"evidence/submission-{args.group}-audit.json", report)
    print(json.dumps({k: report[k] for k in ("status", "validation", "model", "output")}, ensure_ascii=False))
    return 0 if not validation["errors"] else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (ValidationError, KeyError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
