"""Input, rubric and evidence contracts. No model decisions are made here."""
import copy
import hashlib
import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit


class ValidationError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()


def text(value, label, limit=5000, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise ValidationError(f"{label}: требуется текст длиной до {limit} символов")
    return value


def number(value, label, minimum=0, maximum=100):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{label}: требуется конечное число")
    if not minimum <= value <= maximum:
        raise ValidationError(f"{label}: допустимо {minimum}–{maximum}")
    return value


def validate_rubric(raw):
    if not isinstance(raw, dict):
        raise ValidationError("Чек-лист должен быть JSON-объектом")
    result = {"name": text(raw.get("name"), "Название чек-листа", 200), "criteria": []}
    criteria = raw.get("criteria")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 30:
        raise ValidationError("Чек-лист должен содержать от 1 до 30 критериев")
    ids = set()
    for row in criteria:
        if not isinstance(row, dict):
            raise ValidationError("Критерий должен быть объектом")
        cid = text(row.get("id"), "ID критерия", 60)
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", cid) or cid in ids:
            raise ValidationError("ID критериев должны быть уникальными: буквы, цифры, _ и -")
        ids.add(cid)
        kind = row.get("kind")
        if kind not in ("binary", "numeric", "flag"):
            raise ValidationError(f"{cid}: неизвестный тип ответа")
        weight = number(row.get("weight"), "Вес", maximum=10000)
        if kind == "flag" and weight != 0:
            raise ValidationError("Флаг нарушения должен иметь нулевой вес")
        if type(row.get("na_allowed")) is not bool:
            raise ValidationError("na_allowed должен быть true или false")
        item = {"id": cid, "name": text(row.get("name"), "Критерий", 200),
                "section": text(row.get("section", "Общие"), "Секция", 200),
                "kind": kind, "weight": weight,
                "instruction": text(row.get("instruction"), "Правило", 5000),
                "na_allowed": row["na_allowed"],
                "na_rule": text(row.get("na_rule", ""), "Условие N/A", 2000, empty=True)}
        if item["na_allowed"] and not item["na_rule"].strip():
            raise ValidationError("Для N/A укажите условие применимости")
        roles = row.get("positive_evidence_roles", ["operator"])
        if not isinstance(roles, list) or not roles or any(x not in ROLES for x in roles):
            raise ValidationError("Неверные роли положительного свидетельства")
        item["positive_evidence_roles"] = roles
        if kind == "numeric":
            levels = row.get("levels")
            if not isinstance(levels, list) or not 2 <= len(levels) <= 20:
                raise ValidationError("У числовой шкалы должно быть 2–20 ступеней")
            scores = [number(x.get("score"), "Ступень", maximum=10000) for x in levels if isinstance(x, dict)]
            if len(scores) != len(levels) or len(set(scores)) != len(scores) or min(scores) != 0 or max(scores) <= 0:
                raise ValidationError("Шкала должна содержать уникальные ступени, включая 0 и положительный максимум")
            item["levels"] = [{"score": x["score"], "description": text(x.get("description"), "Описание ступени", 2000)} for x in levels]
        result["criteria"].append(item)
    result["id"] = "rubric-" + digest(result)[:16]
    result["hash"] = digest({k: v for k, v in result.items() if k != "id"})
    return result


ROLES = {"operator", "client", "bot", "system"}
ROLE_ALIASES = {"оператор": "operator", "agent": "operator", "клиент": "client", "customer": "client",
                "бот": "bot", "система": "system"}
LINE = re.compile(r"^\s*(?:\[([^\]]+)\]\s*)?([^:：]{1,40})[:：]\s*(.*)$")


def parse_plain(value):
    turns = []
    for line in value.splitlines():
        if not line.strip():
            continue
        match = LINE.match(line)
        if match:
            stamp, role, content = match.groups()
            role = ROLE_ALIASES.get(role.strip().lower(), role.strip().lower())
            if role not in ROLES:
                raise ValidationError(f"Неизвестная роль: {role}. Используйте operator/client/bot/system")
            turns.append({"timestamp": stamp or "", "speaker": role, "text": content})
        elif turns:
            turns[-1]["text"] += "\n" + line
        else:
            raise ValidationError("Текст: [00:00] client: сообщение; каждая новая реплика начинается с роли")
    return turns


def normalize_conversation(raw):
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"transcript": parse_plain(raw)}
        raw = parsed
    if isinstance(raw, list):
        raw = {"transcript": raw}
    if not isinstance(raw, dict):
        raise ValidationError("Диалог должен быть JSON-объектом или текстом с ролями")
    turns = raw.get("transcript")
    if isinstance(turns, str):
        turns = parse_plain(turns)
    if not isinstance(turns, list) or not 1 <= len(turns) <= 1000:
        raise ValidationError("Нужно от 1 до 1000 реплик")
    normalized = []
    for index, message in enumerate(turns):
        if not isinstance(message, dict):
            raise ValidationError("Каждая реплика должна быть объектом")
        speaker = message.get("speaker")
        if speaker not in ROLES:
            raise ValidationError(f"Реплика {index+1}: неизвестная роль; ожидается operator/client/bot/system")
        normalized.append({"id": f"t{index+1:04}", "speaker": speaker,
                           "timestamp": text(message.get("timestamp", ""), "Метка времени", 100, empty=True),
                           "text": text(message.get("text"), "Текст реплики", 20000)})
    if sum(len(x["text"]) for x in normalized) > 48000:
        raise ValidationError("Диалог длиннее 48 000 символов. Не обрезаем текст: сократите ввод или настройте отдельный контур длинного контекста")
    language = raw.get("language", "AUTO")
    if language not in ("RU", "KZ", "EN", "AUTO"):
        raise ValidationError("Язык: RU, KZ, EN или AUTO")
    channel = raw.get("channel", "agent chat")
    if channel not in ("agent chat", "voice call"):
        raise ValidationError("Канал: agent chat или voice call")
    audio = raw.get("audio_url")
    if audio:
        if not isinstance(audio, str) or len(audio) > 10000:
            raise ValidationError("Неверная ссылка на запись")
        url = urlsplit(audio)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValidationError("Запись должна иметь HTTPS-ссылку без встроенного логина")
    source_id = raw.get("conversation_id", "upload-" + digest(normalized)[:12])
    text(source_id, "ID диалога", 200)
    warnings = []
    if not any(t["speaker"] == "operator" for t in normalized):
        warnings.append("В транскрипте нет оператора. Бот не получает баллы за оператора")
    if channel == "voice call":
        warnings.append("Применена выданная рубрика чатов. Дикция и интонация по тексту не оценивались; адаптация рубрики для звонков требует уточнения")
    if any(re.search(r"ignore.{0,30}(instructions|rubric)|игнорируй|поставь.{0,30}(балл|максим)|system prompt", t["text"], re.I) for t in normalized):
        warnings.append("В данных найдена возможная инструкция-ловушка; она не изменяет рубрику")
    result = {"conversation_id": source_id, "language": language, "channel": channel,
              "transcript": normalized, "audio_url": audio or None,
              "source": text(raw.get("source", "uploaded"), "Источник", 100), "warnings": warnings}
    group = raw.get("dataset_group_id")
    if group is not None:
        result["dataset_group_id"] = text(group, "ID набора", 200)
    result["hash"] = digest({k: result[k] for k in ("transcript", "language", "channel")})
    result["id"] = "conv-" + digest([source_id, result["hash"], group])[:20]
    return result


def records_from_payload(raw):
    group = None
    if isinstance(raw, dict) and "isSuccess" in raw:
        if raw.get("isSuccess") is not True:
            raise ValidationError("API организаторов вернул isSuccess != true")
        group = raw.get("data", {}).get("dataset_group_id")
        raw = raw.get("data", {}).get("records")
    elif isinstance(raw, dict) and "records" in raw:
        group = raw.get("dataset_group_id")
        raw = raw["records"]
    if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "transcript" in raw[0]:
        if len(raw) > 500:
            raise ValidationError("Не более 500 диалогов за импорт")
        if group is not None:
            text(group, "ID набора", 200)
            if any(x.get("dataset_group_id") not in (None, group) for x in raw):
                raise ValidationError("ID набора записи не совпадает с API envelope")
            raw = [{**x, "dataset_group_id": group, "source": x.get("source", "organizer")} for x in raw]
        return [normalize_conversation(x) for x in raw]
    return [normalize_conversation(raw)]


def validate_answer(criterion, answer):
    if not isinstance(answer, str):
        raise ValidationError(f"{criterion['id']}: ответ должен быть строкой")
    if answer == "N/A":
        if not criterion["na_allowed"]:
            raise ValidationError(f"{criterion['id']}: N/A запрещён")
        return None
    if criterion["kind"] in ("binary", "flag"):
        if answer not in ("YES", "NO"):
            raise ValidationError(f"{criterion['id']}: допустимы YES/NO")
        return 1 if answer == "YES" else 0
    allowed = {str(x["score"]): x["score"] for x in criterion["levels"]}
    if answer not in allowed:
        raise ValidationError(f"{criterion['id']}: ответ вне заданных ступеней {list(allowed)}")
    return allowed[answer]


def decode_model_json(content):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError("Ответ модели содержит повторяющийся JSON-ключ")
            result[key] = value
        return result
    if not isinstance(content, str):
        raise ValidationError("Модель не вернула JSON-текст")
    return json.loads(content, object_pairs_hook=unique_object)


def materialize_evidence(raw, conversation):
    """AI selects references; exact excerpts and speaker labels come from source."""
    if not isinstance(raw, dict) or not isinstance(raw.get("criteria"), (list, dict)):
        raise ValidationError("Модель не вернула criteria")
    result = copy.deepcopy(raw)
    if isinstance(result["criteria"], dict):
        rows = []
        for cid, row in result["criteria"].items():
            if not isinstance(cid, str) or not isinstance(row, dict) or "criterion_id" in row:
                raise ValidationError("Неверная карта критериев модели")
            rows.append({"criterion_id": cid, **row})
        result["criteria"] = rows
    turns = {t["id"]: t for t in conversation["transcript"]}
    def source_id(ref):
        return f"t{ref:04}" if type(ref) is int else ref
    for row in result["criteria"]:
        if not isinstance(row, dict):
            raise ValidationError("Неверная структура решения модели")
        refs = row.pop("evidence_turns", None)
        if isinstance(refs, list):
            refs = [source_id(ref) for ref in refs]
        coverage = row.get("coverage")
        if isinstance(coverage, dict):
            for key in ("start", "end"):
                coverage[key] = source_id(coverage.get(key))
        if not isinstance(refs, list) or not 1 <= len(refs) <= 8 or any(not isinstance(ref, str) or ref not in turns for ref in refs):
            raise ValidationError("Неверные ID опорных реплик")
        if "evidence" in row:
            raise ValidationError("Модель не должна генерировать текст свидетельств")
        row["evidence"] = []
        for ref in dict.fromkeys(refs):
            turn = turns[ref]
            excerpt = turn["text"][:1000]
            if not excerpt.strip():
                excerpt = turn["text"].lstrip()[:1000]
            row["evidence"].append({"turn_id": ref, "speaker": turn["speaker"], "quote": excerpt})
        row["evidence_origin"] = "SOURCE_TURN_LOOKUP"
    return result


def validate_decisions(raw, rubric, conversation):
    from .rubric import official_rubric
    official = {c["id"]: c for c in official_rubric()["criteria"]}
    if not isinstance(raw, dict) or not isinstance(raw.get("criteria"), list):
        raise ValidationError("Модель не вернула массив criteria")
    rows = raw["criteria"]
    expected = {x["id"] for x in rubric["criteria"]}
    if len(rows) != len(expected) or any(not isinstance(x, dict) for x in rows):
        raise ValidationError("Модель вернула неверное число критериев")
    if {x.get("criterion_id") for x in rows} != expected:
        raise ValidationError("Модель пропустила, продублировала или придумала критерий")
    by_id = {x["criterion_id"]: x for x in rows}
    turns = {t["id"]: t for t in conversation["transcript"]}
    order = {t["id"]: i for i, t in enumerate(conversation["transcript"])}
    checked = []
    for criterion in rubric["criteria"]:
        row = by_id[criterion["id"]]
        answer = row.get("answer")
        score = validate_answer(criterion, answer)
        rationale = text(row.get("rationale"), "Обоснование модели", 4000)
        na_reason = text(row.get("na_reason", ""), "Причина N/A", 2000, empty=True)
        if answer == "N/A" and not na_reason.strip():
            raise ValidationError("N/A без причины")
        if type(row.get("ambiguous")) is not bool:
            raise ValidationError("ambiguous должен быть true или false")
        limitations = row.get("limitations", [])
        if not isinstance(limitations, list) or len(limitations) > 10:
            raise ValidationError("Неверный список ограничений")
        limitations = [text(x, "Ограничение", 1000) for x in limitations]
        coverage = row.get("coverage", {})
        if not isinstance(coverage, dict) or coverage.get("start") not in turns or coverage.get("end") not in turns:
            raise ValidationError("Не указан проверенный участок разговора")
        if order[coverage["start"]] > order[coverage["end"]]:
            raise ValidationError("Проверенный участок задан в обратном порядке")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
            raise ValidationError("Для каждого решения требуется 1–8 точных свидетельств или контекстных реплик")
        clean = []
        for item in evidence:
            if not isinstance(item, dict) or item.get("turn_id") not in turns:
                raise ValidationError("Свидетельство ссылается на несуществующую реплику")
            turn = turns[item["turn_id"]]
            quote = text(item.get("quote"), "Цитата", 4000)
            if quote not in turn["text"]:
                raise ValidationError("Цитата дословно отсутствует в исходной реплике")
            if item.get("speaker") != turn["speaker"]:
                raise ValidationError("Роль в свидетельстве не совпадает с источником")
            if not order[coverage["start"]] <= order[item["turn_id"]] <= order[coverage["end"]]:
                raise ValidationError("Свидетельство вне проверенного участка")
            clean.append({"turn_id": item["turn_id"], "quote": quote, "speaker": turn["speaker"]})
        positive = score is not None and score > 0
        if positive and not any(x["speaker"] in criterion["positive_evidence_roles"] for x in clean):
            raise ValidationError(f"{criterion['id']}: нет свидетельства допустимой роли для положительного решения")
        original = official.get(criterion["id"])
        if original and criterion["instruction"] == original["instruction"]:
            if criterion["id"] == "contact" and answer == "YES":
                first = next((t["id"] for t in conversation["transcript"] if t["speaker"] == "operator"), None)
                if first not in {e["turn_id"] for e in clean}:
                    raise ValidationError("contact: YES требует ссылки на первый ответ оператора; позднее приветствие не подтверждает правило")
            if criterion["id"] == "critical" and answer == "NO" and (coverage["start"] != conversation["transcript"][0]["id"] or coverage["end"] != conversation["transcript"][-1]["id"]):
                raise ValidationError("critical: NO требует coverage всего транскрипта для проверки отсутствия грубых нарушений")
        checked.append({"criterion_id": criterion["id"], "answer": answer, "score": score,
                        "rationale": rationale, "na_reason": na_reason, "coverage": coverage,
                        "evidence": clean, "ambiguous": row["ambiguous"], "limitations": limitations,
                        "evidence_origin": row.get("evidence_origin", "MODEL_QUOTE_VERIFIED"),
                        "evidence_check": "EXACT_TEXT_AND_SPEAKER"})
    return checked


def aggregate(rubric, decisions):
    by_id = {x["criterion_id"]: x for x in decisions}
    if set(by_id) != {c["id"] for c in rubric["criteria"]} or len(decisions) != len(by_id):
        raise ValidationError("Итог требует ровно одно решение для каждого критерия")
    denom = Decimal(0)
    numerator = Decimal(0)
    sections = {}
    contributions = []
    flags = []
    for criterion in rubric["criteria"]:
        row = by_id[criterion["id"]]
        score = validate_answer(criterion, row["answer"])
        if criterion["kind"] == "flag":
            flags.append({"criterion_id": criterion["id"], "answer": row["answer"]})
            continue
        weight = Decimal(str(criterion["weight"]))
        maximum = Decimal(str(max(x["score"] for x in criterion["levels"]))) if criterion["kind"] == "numeric" else Decimal(1)
        earned = Decimal(0) if score is None else weight * Decimal(str(score)) / maximum
        active = Decimal(0) if score is None else weight
        denom += active
        numerator += earned
        section = sections.setdefault(criterion["section"], [Decimal(0), Decimal(0)])
        section[0] += earned
        section[1] += active
        contributions.append({"criterion_id": criterion["id"], "weight": float(weight),
                              "applicable": score is not None, "raw_points": float(earned),
                              "_active": active, "_earned": earned})
    round2 = lambda n: float(n.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    for item in contributions:
        item["effective_weight"] = round2(item.pop("_active") * 100 / denom) if denom else 0.0
        item["contribution"] = round2(item.pop("_earned") * 100 / denom) if denom else 0.0
    return {"total": round2(numerator * 100 / denom) if denom else None,
            "applicable_weight": float(denom), "gross_error": any(x["answer"] == "YES" for x in flags),
            "flags": flags, "contributions": contributions,
            "sections": [{"name": name, "score": round2(a * 100 / b) if b else None,
                          "applicable_weight": float(b)} for name, (a, b) in sections.items()],
            "rounding": "ROUND_HALF_UP, final total only; displayed contributions may sum differently"}


def final_decisions(evaluation, appeals):
    result = copy.deepcopy(evaluation["criteria"])
    by_id = {x["criterion_id"]: x for x in result}
    for appeal in appeals:
        if appeal["status"] == "resolved" and appeal.get("decision") == "change":
            row = by_id[appeal["criterion_id"]]
            row["original_answer"] = row.get("original_answer", row["answer"])
            if appeal.get("revised_decision"):
                row.update(appeal["revised_decision"])
            row["answer"] = appeal["new_answer"]
            criterion = next(x for x in evaluation["rubric"]["criteria"] if x["id"] == appeal["criterion_id"])
            row["score"] = validate_answer(criterion, row["answer"])
            row["review_reason"] = appeal["reason"]
            row["reviewed"] = True
    return result
