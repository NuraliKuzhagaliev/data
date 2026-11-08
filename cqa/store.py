import copy
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .domain import ValidationError, aggregate, final_decisions, text, validate_answer, validate_decisions, validate_rubric
from .prompts import PROMPT_VERSION, prompt_hash
from .rubric import official_rubric


def now():
    return datetime.now(timezone.utc).isoformat()


def source_hash():
    from .domain import digest
    root = Path(__file__).resolve().parent.parent
    paths = sorted([*root.glob("cqa/*.py"), *root.glob("web/*")])
    return digest({p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in paths if p.is_file()})


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.code_hash = source_hash()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS rubrics(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS appeals(id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES evaluations(id), body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS failed_attempts(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        """)
        self.save_rubric(official_rubric())

    def close(self):
        with self.lock:
            self.db.close()

    def _get(self, table, key):
        with self.lock:
            row = self.db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        if not row:
            raise KeyError(key)
        return json.loads(row[0])

    def _list(self, table):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(f"SELECT body FROM {table} ORDER BY rowid DESC")]

    def save_rubric(self, raw):
        rubric = validate_rubric(raw)
        rubric["created_at"] = now()
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO rubrics VALUES (?,?)", (rubric["id"], json.dumps(rubric, ensure_ascii=False)))
        return self.rubric(rubric["id"])

    def rubric(self, key):
        return self._get("rubrics", key)

    def rubrics(self):
        return self._list("rubrics")

    def import_conversations(self, records):
        with self.lock, self.db:
            for record in records:
                self.db.execute("INSERT OR IGNORE INTO conversations VALUES (?,?)", (record["id"], json.dumps(record, ensure_ascii=False)))
        return {"imported": len(records), "ids": [r["id"] for r in records]}

    def conversation(self, key):
        return self._get("conversations", key)

    def conversations(self):
        return self._list("conversations")

    def save_evaluation(self, conversation, rubric, raw, metadata):
        metadata = self._record_rejections(conversation, rubric, metadata)
        try:
            decisions = validate_decisions(raw, rubric, conversation)
        except ValidationError as exc:
            self.record_failure(conversation, rubric, str(exc), metadata, raw)
            raise
        evaluation = {"id": "eval-" + uuid.uuid4().hex, "conversation_id": conversation["id"],
                      "input_source": conversation["source"], "dataset_group_id": conversation.get("dataset_group_id"),
                      "external_conversation_id": conversation["conversation_id"], "transcript_hash": conversation["hash"],
                      "rubric": rubric, "criteria": decisions, "original_summary": aggregate(rubric, decisions),
                      "created_at": now(), "provenance": {**metadata, "prompt_version": PROMPT_VERSION,
                          "prompt_hash": prompt_hash(metadata.get("mode", "evidence")), "code_hash": self.code_hash, "rubric_hash": rubric["hash"]},
                      "warnings": conversation["warnings"]}
        with self.lock, self.db:
            self.db.execute("INSERT INTO evaluations VALUES (?,?,?)", (evaluation["id"], conversation["id"], json.dumps(evaluation, ensure_ascii=False)))
        return self.evaluation(evaluation["id"])

    def record_failure(self, conversation, rubric, error, metadata, raw_response=None):
        metadata = self._record_rejections(conversation, rubric, metadata)
        record = {"conversation_id": conversation["conversation_id"], "transcript_hash": conversation["hash"],
                  "rubric_hash": rubric["hash"], "created_at": now(), "error": error,
                  "provenance": {**metadata, "code_hash": self.code_hash}, "raw_response": raw_response}
        with self.lock, self.db:
            self.db.execute("INSERT INTO failed_attempts VALUES (?,?)", (uuid.uuid4().hex, json.dumps(record, ensure_ascii=False)))

    def _record_rejections(self, conversation, rubric, metadata):
        metadata = dict(metadata)
        for rejected in metadata.pop("rejected_responses", []):
            self.record_failure(conversation, rubric, rejected["error"], rejected.get("metadata", {}), rejected.get("raw_response"))
        return metadata

    def evaluations(self):
        return self._list("evaluations")

    def appeals(self, evaluation_id):
        with self.lock:
            rows = self.db.execute("SELECT body FROM appeals WHERE evaluation_id=? ORDER BY rowid", (evaluation_id,)).fetchall()
        return [json.loads(x[0]) for x in rows]

    def evaluation(self, key):
        evaluation = self._get("evaluations", key)
        appeals = self.appeals(key)
        evaluation["appeals"] = appeals
        evaluation["final_criteria"] = final_decisions(evaluation, appeals)
        evaluation["final_summary"] = aggregate(evaluation["rubric"], evaluation["final_criteria"])
        from .quality import compare_originals, review_priority
        source_id = evaluation["provenance"].get("independent_recheck_of")
        if source_id:
            try:
                evaluation["comparison"] = compare_originals(self._get("evaluations", source_id), evaluation)
            except KeyError:
                evaluation["comparison"] = {"status": "source_unavailable", "changes": []}
        evaluation["quality"] = review_priority(evaluation, self.conversation(evaluation["conversation_id"]))
        return evaluation

    def appeal(self, evaluation_id, criterion_id, comment):
        with self.lock:
            evaluation = self.evaluation(evaluation_id)
            if criterion_id not in {x["criterion_id"] for x in evaluation["criteria"]}:
                raise ValidationError("Критерий отсутствует в оценке")
            if any(x["criterion_id"] == criterion_id and x["status"] == "open" for x in evaluation["appeals"]):
                raise ValidationError("По этому критерию уже открыта апелляция")
            appeal = {"id": "appeal-" + uuid.uuid4().hex, "evaluation_id": evaluation_id,
                      "criterion_id": criterion_id, "comment": text(comment, "Комментарий", 4000),
                      "status": "open", "created_at": now(),
                      "disputed_answer": next(x["answer"] for x in evaluation["final_criteria"] if x["criterion_id"] == criterion_id)}
            with self.db:
                self.db.execute("INSERT INTO appeals VALUES (?,?,?)", (appeal["id"], evaluation_id, json.dumps(appeal, ensure_ascii=False)))
            return appeal

    def resolve(self, key, decision, new_answer, reason, evidence_turns=None):
        with self.lock:
            appeal = self._get("appeals", key)
            if appeal["status"] != "open":
                raise ValidationError("Решение уже сохранено; создайте новую апелляцию")
            if decision not in ("confirm", "change"):
                raise ValidationError("Решение: confirm или change")
            evaluation = self.evaluation(appeal["evaluation_id"])
            criterion = next(c for c in evaluation["rubric"]["criteria"] if c["id"] == appeal["criterion_id"])
            if decision == "confirm":
                new_answer = appeal["disputed_answer"]
            validate_answer(criterion, new_answer)
            reason = text(reason, "Причина решения", 4000)
            revised = None
            if decision == "change":
                conversation = self.conversation(evaluation["conversation_id"])
                if not isinstance(evidence_turns, list) or not 1 <= len(evidence_turns) <= 8 or any(type(n) is not int or not 1 <= n <= len(conversation["transcript"]) for n in evidence_turns):
                    raise ValidationError("Для изменения укажите 1–8 номеров исходных реплик с единицы")
                candidates = copy.deepcopy(evaluation["final_criteria"])
                changed = next(x for x in candidates if x["criterion_id"] == criterion["id"])
                changed.update({"answer": new_answer, "rationale": reason, "na_reason": reason if new_answer == "N/A" else "", "ambiguous": False,
                                "coverage": {"start": conversation["transcript"][0]["id"], "end": conversation["transcript"][-1]["id"]},
                                "evidence": [{"turn_id": conversation["transcript"][n-1]["id"], "speaker": conversation["transcript"][n-1]["speaker"], "quote": conversation["transcript"][n-1]["text"][:2000]} for n in evidence_turns]})
                revised = next(x for x in validate_decisions({"criteria": candidates}, evaluation["rubric"], conversation) if x["criterion_id"] == criterion["id"])
            appeal.update({"status": "resolved", "decision": decision, "new_answer": new_answer,
                           "reason": reason, "resolved_at": now(), "revised_decision": revised})
            with self.db:
                self.db.execute("UPDATE appeals SET body=? WHERE id=?", (json.dumps(appeal, ensure_ascii=False), key))
            return self.evaluation(appeal["evaluation_id"])
