"""Explain review priority using exact rubric arithmetic, never a confidence score."""
import copy

from .domain import aggregate
from .rubric import official_rubric


def compare_originals(original, repeated):
    if original["rubric"]["hash"] != repeated["rubric"]["hash"] or original["transcript_hash"] != repeated["transcript_hash"]:
        return {"status": "incompatible", "reason": "Изменились источник или рубрика", "changes": []}
    old = {c["criterion_id"]: c for c in original["criteria"]}
    changes = [{"criterion_id": c["criterion_id"], "before": old[c["criterion_id"]]["answer"], "after": c["answer"]}
               for c in repeated["criteria"] if old[c["criterion_id"]]["answer"] != c["answer"]]
    before, after = original["original_summary"]["total"], repeated["original_summary"]["total"]
    return {"status": "requires_review" if changes else "agreement", "source_evaluation_id": original["id"],
            "repeated_evaluation_id": repeated["id"], "changes": changes, "changed_criteria": len(changes),
            "original_total": before, "repeated_total": after,
            "total_delta": round(after-before, 2) if before is not None and after is not None else None,
            "method": "new_request_without_prior_answers; same_source_and_rubric", "human_verified": False,
            "note": "Совпадение двух автоматических оценок не доказывает их правильность. Расхождения требуют пересмотра."}


def review_priority(evaluation, conversation):
    rubric = evaluation["rubric"]
    decisions = evaluation.get("final_criteria", evaluation["criteria"])
    current = aggregate(rubric, decisions)["total"]
    by_id = {c["criterion_id"]: c for c in decisions}
    canonical = {c["id"]: c for c in official_rubric()["criteria"]}
    turns = conversation["transcript"]
    first_operator = next((t["id"] for t in turns if t["speaker"] == "operator"), None)
    changed = {c["criterion_id"] for c in evaluation.get("comparison", {}).get("changes", [])}
    rows = []
    for criterion in rubric["criteria"]:
        cid = criterion["id"]; decision = by_id[cid]
        answers = [str(l["score"]) for l in criterion["levels"]] if criterion["kind"] == "numeric" else ["YES", "NO"]
        if criterion["na_allowed"]: answers.append("N/A")
        alternatives = []
        for answer in answers:
            alternate = copy.deepcopy(decisions)
            next(c for c in alternate if c["criterion_id"] == cid)["answer"] = answer
            total = aggregate(rubric, alternate)["total"]
            alternatives.append({"answer": answer, "total": total,
                                 "delta": round(total-current, 2) if total is not None and current is not None else None})
        impact = max((abs(a["delta"]) for a in alternatives if a["delta"] is not None), default=0)
        reasons = []
        if decision.get("ambiguous"): reasons.append("Модель отметила неоднозначность")
        if decision.get("limitations"): reasons.append("Указаны ограничения проверки")
        if cid in changed: reasons.append("Повторная оценка дала другой ответ")
        gross_error = criterion["kind"] == "flag" and decision["answer"] == "YES"
        if gross_error: reasons.append("Выявлено грубое нарушение")
        original = canonical.get(cid)
        boundary_issue = False
        if original and criterion["instruction"] == original["instruction"]:
            if cid == "critical" and decision["answer"] == "NO" and decision["coverage"] != {"start": turns[0]["id"], "end": turns[-1]["id"]}:
                reasons.append("Архивная оценка: указан неполный охват проверки отсутствия нарушений")
                boundary_issue = True
            if cid == "contact" and decision["answer"] == "YES" and first_operator not in {e["turn_id"] for e in decision["evidence"]}:
                reasons.append("Положительный контакт не ссылается на первый ответ оператора")
                boundary_issue = True
        rows.append({"criterion_id": cid, "name": criterion["name"], "answer": decision["answer"],
                     "needs_review": bool(reasons), "reasons": reasons, "boundary_issue": boundary_issue,
                     "gross_error": gross_error, "max_score_change": impact, "alternatives": alternatives})
    rows.sort(key=lambda r: (r["gross_error"] or r["boundary_issue"], r["needs_review"], r["max_score_change"]), reverse=True)
    return {"current_total": current, "review_count": sum(r["needs_review"] for r in rows), "items": rows,
            "method": "one_criterion_counterfactual; other_answers_fixed; exact_official_weights_and_NA",
            "calibrated_confidence": None, "is_accuracy_estimate": False,
            "note": "Влияние на итог рассчитано при неизменных остальных ответах. Это не вероятность ошибки и не экспертная оценка."}
