"""Group-aware DEV split manifest. Never use this to tune on control chats."""
import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cqa.domain import digest, records_from_payload


def split(payload, seed=42):
    if isinstance(payload, dict) and payload.get("data", {}).get("dataset_group_id") not in (None, "DEV"):
        raise ValueError("Разделение доступно только для DEV; контрольные данные не используются для настройки")
    records = records_from_payload(payload)
    parents = list(range(len(records)))
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]; i = parents[i]
        return i
    tokens = []
    for record in records:
        value = " ".join(t["speaker"] + " " + t["text"].lower() for t in record["transcript"])
        value = re.sub(r"\d+", "#", value)
        words = re.findall(r"[\w#]+", value)
        tokens.append(set(tuple(words[i:i+3]) for i in range(max(1, len(words)-2))))
    for i in range(len(records)):
        for j in range(i):
            similarity = len(tokens[i] & tokens[j]) / max(1, len(tokens[i] | tokens[j]))
            if records[i]["hash"] == records[j]["hash"] or records[i]["conversation_id"] == records[j]["conversation_id"] or similarity >= .75:
                parents[find(i)] = find(j)
    groups = {}
    for i, record in enumerate(records):
        groups.setdefault(find(i), []).append(record)
    values = list(groups.values()); random.Random(seed).shuffle(values)
    partitions = {"development": [], "calibration": [], "holdout": []}
    # Whole groups only, including oversized groups. Report the resulting counts.
    for group in values:
        name = "development" if len(partitions["development"]) < len(records)*.6 else "calibration" if len(partitions["calibration"]) < len(records)*.2 else "holdout"
        partitions[name].extend({"conversation_id": r["conversation_id"], "hash": r["hash"], "language": r["language"], "group": "group-" + str(find(records.index(r)))} for r in group)
    return {"seed": seed, "dataset_hash": digest(payload), "group_rule": "Exact hash / same source ID / normalized trigram Jaccard >= 0.75", "group_count": len(groups),
            "warning": "Эвристика не обнаруживает все смысловые или переводные дубликаты. До обучения проверьте родственные сценарии вручную. Human GT отсутствует; split не создаёт разметку.",
            "partitions": partitions, "counts": {k: len(v) for k, v in partitions.items()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--dataset", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(); payload = json.loads(Path(args.dataset).read_text(encoding="utf-8-sig"))
    result = split(payload); Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"groups": result["group_count"], "counts": result["counts"]}))
