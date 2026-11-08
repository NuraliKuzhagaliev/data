"""Diagnostic evaluation. These metrics are not the unknown organizer metric."""
import math
import statistics
from collections import defaultdict

from .domain import ValidationError, aggregate
from .rubric import official_rubric


def _records(payload):
    if isinstance(payload, dict) and "results" in payload:
        payload = payload["results"]
    if not isinstance(payload, list):
        raise ValidationError("Ожидается массив прогнозов или объект results")
    return payload


def _row(record):
    if not isinstance(record, dict):
        raise ValidationError("Запись метрик должна быть объектом")
    cid = record.get("external_conversation_id", record.get("conversation_id"))
    if not isinstance(cid, str) or not cid.strip():
        raise ValidationError("В метриках требуется непустой ID")
    specs = {c["id"]: c for c in official_rubric()["criteria"]}
    values = {}
    criteria = record.get("criteria", [])
    if not isinstance(criteria, list) or any(not isinstance(c, dict) for c in criteria):
        raise ValidationError("criteria в метриках должен быть массивом объектов")
    for c in criteria:
        if "value" in c:
            value = c["value"]
        else:
            answer = c.get("answer")
            try:
                value = None if answer == "N/A" else True if answer == "YES" else False if answer == "NO" else float(answer)
            except (TypeError, ValueError):
                raise ValidationError("Недопустимый ответ в метриках") from None
        spec = specs.get(c.get("criterion_id"))
        if not spec:
            raise ValidationError("Неизвестный критерий в метриках")
        if value is None:
            valid = spec["na_allowed"]
        elif spec["kind"] != "numeric":
            valid = type(value) is bool
        else:
            valid = type(value) in (int, float) and math.isfinite(value) and value in [x["score"] for x in spec["levels"]]
        if not valid:
            raise ValidationError("Недопустимая шкала или тип: " + spec["id"])
        if c["criterion_id"] in values:
            raise ValidationError("Повтор критерия в метриках")
        values[c["criterion_id"]] = value
    summary = record.get("original_summary", {})
    if not isinstance(summary, dict):
        raise ValidationError("Неверная структура итога в метриках")
    total = record.get("score", {}).get("final_score") if isinstance(record.get("score"), dict) else summary.get("total")
    if total is not None and (type(total) not in (int, float) or not math.isfinite(total) or not 0 <= total <= 100):
        raise ValidationError("Итог в метриках должен быть числом 0–100")
    if record.get("status") == "error" and (values or total is not None):
        raise ValidationError("Ошибка обработки не может содержать оценку")
    if set(values) == set(specs) and total is not None:
        answers = [{"criterion_id": k, "answer": "N/A" if v is None else "YES" if v is True else "NO" if v is False else str(int(v))} for k, v in values.items()]
        if total != aggregate(official_rubric(), answers)["total"]:
            raise ValidationError("Итог в метриках не соответствует критериям")
    return cid, values, total


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def diagnostics(predictions, truth, tolerance=1):
    if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValidationError("Допуск должен быть конечным неотрицательным числом")
    provenance = truth.get("provenance", {}) if isinstance(truth, dict) else {}
    label_source = provenance.get("label_source", "UNKNOWN")
    if label_source not in ("human", "synthetic", "pseudo", "UNKNOWN"):
        raise ValidationError("label_source: human, synthetic, pseudo или UNKNOWN")
    labels = {}
    label_hashes = {}
    for record in _records(truth):
        cid, values, total = _row(record)
        if cid in labels:
            raise ValidationError("В эталоне повторяется ID")
        labels[cid] = (values, total)
        label_hashes[cid] = record.get("transcript_hash")
    rubric = official_rubric()
    specs = {c["id"]: c for c in rubric["criteria"]}
    runs = defaultdict(list)
    for record in _records(predictions):
        _row(record)
        p = record.get("provenance", {})
        if not isinstance(p, dict):
            raise ValidationError("provenance должен быть объектом")
        if p.get("is_mock"):
            raise ValidationError("MOCK не допускается к измерению точности модели")
        key = tuple(p.get(field, "UNKNOWN") for field in ("provider", "model", "prompt_hash", "rubric_hash", "mode", "code_hash"))
        runs[key].append(record)
    if not runs:
        runs[("UNKNOWN",)*6] = []
    reports = []
    for key, records in runs.items():
        errors = defaultdict(lambda: defaultdict(list))
        exact = defaultdict(lambda: defaultdict(list))
        na_mismatch = defaultdict(int)
        exclusions = defaultdict(int)
        totals_by_id = defaultdict(list)
        total_matches = defaultdict(list)
        total_tolerance = defaultdict(list)
        repeats = defaultdict(list)
        predictions_used = set()
        failure_count = 0
        missed_ids = []
        discrepancy_rows = []
        input_hashes = {}
        hash_pairs_verified = 0
        tp = fp = fn = 0
        for record in records:
            cid, values, total = _row(record)
            if cid not in labels:
                missed_ids.append(cid)
                continue
            transcript_hash = record.get("transcript_hash")
            if transcript_hash:
                if cid in input_hashes and input_hashes[cid] != transcript_hash:
                    raise ValidationError("Разные версии транскрипта с одним ID нельзя считать повторами")
                input_hashes[cid] = transcript_hash
                if label_hashes[cid]:
                    if transcript_hash != label_hashes[cid]:
                        raise ValidationError("Хеш транскрипта прогноза не совпадает с эталоном: " + cid)
                    hash_pairs_verified += 1
            predictions_used.add(cid)
            if record.get("status") == "error":
                failure_count += 1
            expected, expected_total = labels[cid]
            discrepancies = []
            for criterion_id, actual in expected.items():
                if criterion_id not in specs:
                    raise ValidationError("Эталон содержит неизвестный критерий: " + criterion_id)
                if criterion_id not in values:
                    exact[criterion_id][cid].append(False)
                    exclusions[criterion_id] += 1
                    discrepancies.append({"criterion": criterion_id, "type": "missing_prediction"})
                    continue
                predicted = values[criterion_id]
                same = type(predicted) is type(actual) and predicted == actual if isinstance(actual, bool) or isinstance(predicted, bool) else predicted == actual
                exact[criterion_id][cid].append(same)
                if actual is None or predicted is None:
                    na_mismatch[criterion_id] += int(not same)
                    exclusions[criterion_id] += 1
                else:
                    errors[criterion_id][cid].append(float(predicted) - float(actual))
                if not same:
                    discrepancies.append({"criterion": criterion_id, "truth": actual, "prediction": predicted, "type": "REQUIRES_ERROR_ANALYSIS"})
            if expected_total is not None:
                total_matches[cid].append(total == expected_total)
                total_tolerance[cid].append(total is not None and abs(total - expected_total) <= tolerance)
            if total is not None and expected_total is not None:
                totals_by_id[cid].append(total - expected_total)
            repeats[cid].append((values, total))
            if discrepancies:
                discrepancy_rows.append({"conversation_id": cid, "errors": discrepancies})
        for cid, (expected, expected_total) in labels.items():
            if cid not in predictions_used:
                for criterion_id in expected:
                    exact[criterion_id][cid].append(False)
                    exclusions[criterion_id] += 1
                if expected_total is not None:
                    total_matches[cid].append(False)
                    total_tolerance[cid].append(False)
                discrepancy_rows.append({"conversation_id": cid, "errors": [{"type": "missing_prediction"}]})
            if "critical" in expected:
                attempts = repeats.get(cid, [({}, None)])
                weight = 1 / len(attempts)
                for values, _ in attempts:
                    tp += weight * int(expected["critical"] is True and values.get("critical") is True)
                    fp += weight * int(expected["critical"] is False and values.get("critical") is True)
                    fn += weight * int(expected["critical"] is True and values.get("critical") is not True)
        mean = lambda values: statistics.mean(values) if values else None
        macro = lambda groups, transform=lambda x: x: mean([statistics.mean(transform(x) for x in xs) for xs in groups.values() if xs])
        per_criterion = []
        for cid, c in specs.items():
            maximum = max(x["score"] for x in c["levels"]) if c["kind"] == "numeric" else 1
            nums, matches = errors[cid], exact[cid]
            per_criterion.append({"criterion_id": cid, "weight": c["weight"], "scale_max": maximum,
                                  "n_compared": len(matches), "exact_match": macro(matches),
                                  "n_numeric_pairs": sum(map(len, nums.values())), "n_numeric_conversations": len(nums),
                                  "mae_raw": macro(nums, abs), "mae_normalized": macro(nums, lambda x: abs(x)/maximum),
                                  "signed_bias_raw": macro(nums),
                                  "na_mismatches": na_mismatch[cid], "excluded_numeric_pairs": exclusions[cid]})
        repeat_info = []
        for cid, values in repeats.items():
            if len(values) < 2:
                continue
            ts = [v[1] for v in values if v[1] is not None]
            repeat_info.append({"conversation_id": cid, "n": len(values),
                                "total_stddev": statistics.pstdev(ts) if ts else None,
                                "total_range": max(ts) - min(ts) if ts else None,
                                "criteria_flips": sum(len({str(v[0].get(c)) for v in values}) > 1 for c in specs)})
        latency = [r["provenance"]["latency_ms"] for r in records if r.get("provenance", {}).get("latency_ms") is not None]
        costs = [r["provenance"]["api_cost_usd"] for r in records if r.get("provenance", {}).get("api_cost_usd") is not None]
        reports.append({"provider": key[0], "model": key[1], "prompt_hash": key[2], "rubric_hash": key[3], "mode": key[4], "code_hash": key[5],
                        "aggregation": "macro_by_conversation_then_mean_over_repeats; missing exact=0; MAE conditional on numeric pairs",
                        "unique_labelled_conversations": len(predictions_used), "records": len(records), "failures": failure_count,
                        "input_identity": {"hash_matched_records": hash_pairs_verified, "status": "VERIFIED_HASH_MATCH" if records and hash_pairs_verified == len(records) else "NOT_FULLY_VERIFIED; IDs matched, hashes missing for some records"},
                        "missing_prediction_ids": sorted(set(labels) - predictions_used), "unlabelled_prediction_ids": sorted(set(missed_ids)),
                        "per_criterion": per_criterion,
                        "total": {"n": sum(map(len, totals_by_id.values())), "n_expected_conversations": len(total_matches), "n_numeric_conversations": len(totals_by_id),
                                  "numeric_coverage": len(totals_by_id)/len(total_matches) if total_matches else None,
                                  "mae": macro(totals_by_id, abs),
                                  "rmse": math.sqrt(macro(totals_by_id, lambda x: x*x)) if totals_by_id else None,
                                  "signed_bias": macro(totals_by_id),
                                  "exact_match": macro(total_matches), "within_tolerance": macro(total_tolerance),
                                  "tolerance": tolerance},
                        "critical": {"tp": tp, "fp": fp, "fn": fn, "precision": tp/(tp+fp) if tp+fp else None, "recall": tp/(tp+fn) if tp+fn else None},
                        "latency_ms": {"n": len(latency), "median": statistics.median(latency) if latency else None, "p95": percentile(latency, .95)},
                        "api_cost_usd": {"n_known": len(costs), "total_known": sum(costs) if costs else None},
                        "repeatability": repeat_info, "discrepancies": discrepancy_rows})
    return {"metric_source": "DIAGNOSTIC_NOT_OFFICIAL", "label_source": label_source,
            "accuracy_claim_allowed": label_source == "human" and bool(labels) and all(set(v)==set(specs) and t is not None for v,t in labels.values()), "human_provenance_verified": False,
            "warning": "label_source задаётся владельцем разметки; программа не доказывает её человеческое происхождение. Разброс повторов не доказывает точность. Ошибка при N/A учитывается отдельно от числовой MAE. Малая выборка ограничивает выводы.",
            "runs": reports}
