import argparse
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="CQA Evidence · локальное приложение и проверка результатов")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--db")
    serve.add_argument("--data-dir")
    check = commands.add_parser("check-submission")
    check.add_argument("--file", required=True)
    check.add_argument("--dataset", required=True)
    check.add_argument("--group", choices=("dev", "prod"), required=True)
    export = commands.add_parser("export-job")
    export.add_argument("--job", required=True)
    export.add_argument("--group", choices=("dev", "prod"), required=True)
    export.add_argument("--team", required=True)
    build = commands.add_parser("build-submission")
    build.add_argument("--predictions", required=True)
    build.add_argument("--dataset", required=True)
    build.add_argument("--group", choices=("dev", "prod"), required=True)
    build.add_argument("--team", required=True)
    build.add_argument("--output")
    batch = commands.add_parser("batch")
    batch.add_argument("--dataset", required=True)
    batch.add_argument("--provider", choices=("groq", "gemini", "llamacpp", "ollama"), default="groq")
    batch.add_argument("--model")
    batch.add_argument("--mode", choices=("baseline", "evidence"), default="evidence")
    batch.add_argument("--output", required=True)
    batch.add_argument("--limit", type=int)
    batch.add_argument("--repeat", type=int, default=1)
    batch.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.command == "serve":
        from .server import Application, create_server
        app = Application(args.db, data_dir=args.data_dir)
        server = create_server(app, port=args.port)
        print(f"CQA Evidence: http://127.0.0.1:{server.server_port} · Ctrl+C для остановки", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            app.store.close()
        return 0
    if args.command == "check-submission":
        from .domain import records_from_payload, ValidationError
        from .exporting import validate_submission
        records = records_from_payload(json.loads(Path(args.dataset).read_text(encoding="utf-8-sig")))
        expected = 65 if args.group == "dev" else 10
        if len(records) != expected:
            raise ValidationError(f"{args.group}: требуется {expected} исходных записей")
        payload = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
        result = validate_submission(payload, [r["conversation_id"] for r in records], {r["conversation_id"]: len(r["transcript"]) for r in records})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["errors"] == 0 else 2
    if args.command == "export-job":
        from .server import Application, ROOT
        app = Application()
        payload = app.submission_for_job(args.job, args.group, args.team)
        target = ROOT / "results" / args.group / "results.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(target))
        return 0
    if args.command == "build-submission":
        from .domain import records_from_payload
        from .exporting import submission, validate_submission
        records = records_from_payload(json.loads(Path(args.dataset).read_text(encoding="utf-8-sig")))
        outcomes = json.loads(Path(args.predictions).read_text(encoding="utf-8-sig"))
        payload = submission(args.team, outcomes, args.group, [r["conversation_id"] for r in records])
        validate_submission(payload, [r["conversation_id"] for r in records], {r["conversation_id"]:len(r["transcript"]) for r in records})
        target = Path(args.output or f"results/{args.group}/results.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(target)
        print(str(target.resolve()))
        return 0
    if args.command == "batch":
        from .domain import records_from_payload
        from .server import Application, ROOT
        app = Application(data_dir=ROOT / ".local/batch-state", config_path=ROOT / ".local/provider.json")
        records = records_from_payload(json.loads(Path(args.dataset).read_text(encoding="utf-8-sig")))
        if args.limit is not None and not 1 <= args.limit <= 500:
            parser.error("limit: 1–500")
        if args.limit:
            records = records[:args.limit]
        if not 1 <= args.repeat <= 10:
            parser.error("repeat: 1–10")
        app.store.import_conversations(records)
        provider = app.provider(args.provider, args.model or app.config["model"])
        from .rubric import official_rubric
        rubric = app.store.rubric(official_rubric()["id"])
        outcomes = []
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(target.read_text(encoding="utf-8")) if args.resume and target.exists() else []
        from .store import source_hash
        current_code = source_hash()
        for repeat in range(args.repeat):
            for c in records:
                import time
                from .prompts import prompt_hash
                started = time.perf_counter()
                metadata = {"provider": args.provider, "model": provider.model, "mode": args.mode, "prompt_hash": prompt_hash(args.mode), "rubric_hash": rubric["hash"], "is_mock": False}
                cached = next((r for r in previous if r.get("external_conversation_id") == c["conversation_id"] and r.get("transcript_hash") == c["hash"] and r.get("repeat", 0) == repeat and r.get("provenance", {}).get("code_hash") == current_code and all(r.get("provenance", {}).get(k) == metadata[k] for k in ("provider", "model", "prompt_hash", "rubric_hash", "mode"))), None)
                if cached:
                    outcomes.append(cached)
                    print(json.dumps({"id":c["conversation_id"], "status":"reused", "repeat":repeat}),flush=True)
                    continue
                got_response = False
                try:
                    raw, metadata = provider.evaluate(rubric, c, mode=args.mode)
                    got_response = True
                    result = app.store.save_evaluation(c, rubric, raw, metadata)
                    result["repeat"] = repeat
                except Exception as exc:
                    from .domain import ValidationError
                    from .providers import ProviderError
                    metadata.update({"provider": args.provider, "mode": args.mode, "prompt_hash": prompt_hash(args.mode), "rubric_hash": rubric["hash"], "is_mock": False})
                    metadata.setdefault("latency_ms", round((time.perf_counter() - started)*1000, 2))
                    result = {"conversation_id": c["conversation_id"], "status": "error", "error": str(exc) if isinstance(exc, (ValidationError, ProviderError)) else type(exc).__name__, "repeat": repeat,
                              "provenance": metadata}
                    if not got_response:
                        app.store.record_failure(c, rubric, result["error"], metadata)
                outcomes.append(result)
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(json.dumps(outcomes, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(target)
                print(json.dumps({"id": c["conversation_id"], "status": result.get("status", "ok"), "repeat": repeat, "completed": len(outcomes), "total": len(records) * args.repeat}, ensure_ascii=False), flush=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(outcomes, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
        return 0 if all(x.get("status") != "error" for x in outcomes) else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
