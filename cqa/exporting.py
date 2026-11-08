"""Official submission schema 1.0, separate from the richer audit export."""
import csv
import io
from .domain import ValidationError, aggregate, validate_answer
from .rubric import official_rubric


def official_record(evaluation, use_final=False):
    if evaluation["provenance"].get("is_mock"):
        raise ValidationError("MOCK запрещён в официальной выгрузке")
    if evaluation.get("input_source") == "synthetic":
        raise ValidationError("Собственные стресс-сценарии не являются набором организаторов и не экспортируются для сдачи")
    rubric = evaluation["rubric"]
    expected = official_rubric()
    if len(rubric["criteria"]) != 8 or {c["id"] for c in rubric["criteria"]} != {c["id"] for c in expected["criteria"]}:
        raise ValidationError("Официальная выгрузка требует 8 кодов критериев из приложения к гайду")
    for criterion in rubric["criteria"]:
        spec = next(c for c in expected["criteria"] if c["id"] == criterion["id"])
        if any(criterion.get(k) != spec.get(k) for k in ("kind", "weight", "na_allowed")):
            raise ValidationError("Выгрузка 1.0: типы, веса и N/A должны соответствовать действующему официальному контракту")
        if criterion["kind"] == "numeric" and [x["score"] for x in criterion["levels"]] != [x["score"] for x in spec["levels"]]:
            raise ValidationError("Выгрузка 1.0: ступени шкал не соответствуют приложению")
        if any(criterion.get(k) != spec.get(k) for k in ("instruction", "na_rule", "positive_evidence_roles", "levels")):
            raise ValidationError("Для сдачи примените актуальные официальные правила, условия N/A и описания ступеней. Изменённый чек-лист можно сохранить в подробный JSON/CSV-аудит")
    decisions = evaluation["final_criteria"] if use_final else evaluation["criteria"]
    summary = aggregate(rubric, decisions)
    rows = []
    for row in decisions:
        c = next(c for c in rubric["criteria"] if c["id"] == row["criterion_id"])
        score = validate_answer(c, row["answer"])
        value = None if score is None else bool(score) if c["kind"] in ("binary", "flag") else score
        rationale = row.get("review_reason") if use_final and row.get("reviewed") else row["rationale"]
        if row["answer"] == "N/A":
            rationale += " N/A: " + row.get("na_reason", rationale)
        rows.append({"criterion_id": row["criterion_id"], "value": value,
                     "rationale": rationale, "evidence_turns": sorted({int(x["turn_id"][1:]) for x in row["evidence"]})})
    return {"conversation_id": evaluation["external_conversation_id"], "status": "ok", "criteria": rows,
            "score": {"final_score": summary["total"], "critical_flag": summary["gross_error"]}}


def submission(team_name, outcomes, dataset, expected_ids, use_final=False):
    if not isinstance(team_name, str) or not team_name.strip() or len(team_name) > 200:
        raise ValidationError("Укажите название команды")
    if dataset not in ("dev", "prod"):
        raise ValidationError("Набор: dev или prod")
    expected_ids = list(expected_ids)
    if len(expected_ids) != len(set(expected_ids)):
        raise ValidationError("В исходном наборе повторяются conversation_id")
    expected_count = 65 if dataset == "dev" else 10
    if len(expected_ids) != expected_count:
        raise ValidationError(f"Для {dataset.upper()} требуется ровно {expected_count} исходных ID")
    records = []
    models = set()
    modes = set()
    for outcome in outcomes:
        if outcome.get("status") == "error":
            records.append({"conversation_id": outcome["conversation_id"], "status": "error", "criteria": [],
                            "score": None, "error": outcome.get("error", "Не удалось получить ответ модели")})
        else:
            records.append(official_record(outcome, use_final=use_final))
            meta = outcome["provenance"]
            models.add(meta["model"])
            modes.add("external_api" if meta["provider"] in ("groq", "gemini") else "local")
    ids = [x["conversation_id"] for x in records]
    if len(ids) != len(set(ids)) or set(ids) != set(expected_ids):
        raise ValidationError("ID результатов должны точно совпадать с исходным набором без повторов и пропусков")
    mode = next(iter(modes)) if len(modes) == 1 else "hybrid" if modes else "local"
    result = {"schema_version": "1.0", "team_name": team_name.strip(),
              "model": {"mode": mode, "name": "; ".join(sorted(models)) or "NO_SUCCESSFUL_MODEL_CALL"},
              "results": records}
    validate_submission(result, expected_ids)
    return result


def validate_submission(payload, expected_ids, turn_counts=None):
    expected_ids = list(expected_ids)
    if len(expected_ids) != len(set(expected_ids)):
        raise ValidationError("Исходный набор содержит повтор conversation_id")
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
        raise ValidationError("schema_version должен быть 1.0")
    if not isinstance(payload.get("team_name"), str) or not payload["team_name"].strip():
        raise ValidationError("team_name обязателен")
    model = payload.get("model", {})
    if model.get("mode") not in ("local", "external_api", "hybrid") or not isinstance(model.get("name"), str) or not model["name"]:
        raise ValidationError("Некорректный model")
    rows = payload.get("results")
    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
        raise ValidationError("results должен быть массивом объектов")
    ids = [x.get("conversation_id") for x in rows]
    if any(not isinstance(x, str) for x in ids) or len(ids) != len(set(ids)) or set(ids) != set(expected_ids):
        raise ValidationError("Неполный набор или повтор ID в results")
    rubric = official_rubric()
    expected = {c["id"]: c for c in rubric["criteria"]}
    for row in rows:
        if row.get("status") == "error":
            if row.get("criteria") != [] or row.get("score") is not None or not isinstance(row.get("error"), str) or not row["error"].strip():
                raise ValidationError("Неверная запись error")
            continue
        if row.get("status") != "ok" or not isinstance(row.get("criteria"), list):
            raise ValidationError("status должен быть ok или error")
        criteria = row["criteria"]
        if len(criteria) != 8 or any(not isinstance(c, dict) for c in criteria) or {c.get("criterion_id") for c in criteria} != set(expected):
            raise ValidationError("При ok нужны все 8 критериев ровно по одному разу")
        decisions = []
        for item in criteria:
            c = expected[item["criterion_id"]]
            value = item.get("value")
            if value is None:
                answer = "N/A"
            elif c["kind"] in ("binary", "flag"):
                if type(value) is not bool:
                    raise ValidationError("Для бинарного критерия нужен boolean")
                answer = "YES" if value else "NO"
            else:
                if type(value) not in (int, float) or value not in [x["score"] for x in c["levels"]]:
                    raise ValidationError("Числовая оценка должна быть ступенью шкалы")
                answer = str(int(value)) if float(value).is_integer() else str(value)
            validate_answer(c, answer)
            evidence = item.get("evidence_turns")
            if not isinstance(evidence, list) or not evidence or any(type(n) is not int or n < 1 for n in evidence):
                raise ValidationError("evidence_turns: непустой массив целых номеров с 1")
            if turn_counts and any(n > turn_counts[row["conversation_id"]] for n in evidence):
                raise ValidationError("evidence_turns за пределами исходного массива")
            if not isinstance(item.get("rationale"), str) or not item["rationale"].strip():
                raise ValidationError("Обоснование обязательно")
            decisions.append({"criterion_id": c["id"], "answer": answer})
        summary = aggregate(rubric, decisions)
        score = row.get("score")
        if not isinstance(score, dict) or type(score.get("final_score")) not in (int, float) or score.get("final_score") != summary["total"] or type(score.get("critical_flag")) is not bool or score.get("critical_flag") != summary["gross_error"]:
            raise ValidationError("Итог или critical_flag не соответствует критериям")
    return {"status": "PASS", "records": len(rows), "ok": sum(x["status"] == "ok" for x in rows),
            "errors": sum(x["status"] == "error" for x in rows)}


def csv_export(evaluation):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["criterion_id", "criterion", "answer_original", "answer_final", "weight", "explanation", "turns", "review_reason"])
    originals = {x["criterion_id"]: x for x in evaluation["criteria"]}
    for row in evaluation["final_criteria"]:
        c = next(c for c in evaluation["rubric"]["criteria"] if c["id"] == row["criterion_id"])
        values = [c["id"], c["name"], originals[c["id"]]["answer"], row["answer"], c["weight"], row["rationale"],
                  ";".join(x["turn_id"] for x in row["evidence"]), row.get("review_reason", "")]
        writer.writerow(["'" + x if isinstance(x, str) and x.startswith(("=", "+", "-", "@", "\t", "\r")) else x for x in values])
    return "\ufeff" + stream.getvalue()
