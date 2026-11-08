"""Local-only live provider and an explicitly labelled UI contract fixture."""
import json
import math
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .domain import ValidationError, digest, materialize_evidence, validate_decisions, decode_model_json
from .prompts import BASELINE_SYSTEM, COMPACT_AFTER_TURNS, messages, output_schema


class ProviderError(RuntimeError):
    def __init__(self, message, *, code="provider_error", retry_after=None, metadata=None, raw_response=None):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after
        self.metadata = metadata or {}
        self.raw_response = raw_response


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("Редирект AI-провайдера запрещён; данные не пересылаются другому адресу", code="redirect_blocked")


def safe_open(request, **kwargs):
    # Disable ambient proxies as well as redirects, including loopback traffic.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(request, **kwargs)


def local_endpoint(value):
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValidationError("Разрешён только локальный HTTP AI-сервер на loopback без пути и параметров")
    return value.rstrip("/")


def read_json(response, maximum=2_000_000):
    value = response.read(maximum + 1)
    if len(value) > maximum:
        raise ProviderError("Ответ провайдера превышает ограничение размера")
    try:
        return json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderError("Провайдер вернул невалидный JSON") from None


def rate_headers(response):
    return {name: response.headers.get(name) for name in (
        "retry-after", "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens")}


class OllamaProvider:
    name = "ollama"
    is_mock = False

    def __init__(self, endpoint="http://127.0.0.1:11434", model="", timeout=120, opener=None):
        self.endpoint = local_endpoint(endpoint)
        self.model = model
        self.timeout = timeout
        self.opener = opener or safe_open

    def models(self):
        try:
            with self.opener(self.endpoint + "/api/tags", timeout=3) as response:
                data = read_json(response)
            if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                raise ProviderError("Неверный ответ списка моделей")
            return [{"name": x.get("name", ""), "digest": x.get("digest"), "size": x.get("size"),
                     "remote": bool(x.get("remote_host") or x.get("remote_model") or "cloud" in x.get("name", "").lower())}
                    for x in data["models"] if isinstance(x, dict)]
        except (OSError, ValueError, ProviderError):
            return []

    def evaluate(self, rubric, conversation, mode="evidence"):
        models = self.models()
        selected = next((x for x in models if x["name"] == self.model), None)
        if not selected:
            raise ProviderError("Локальная модель недоступна. Запустите локальный сервер и выберите загруженную модель. Успешная оценка не создана")
        if selected["remote"]:
            raise ProviderError("Облачная модель через локальный сервер заблокирована: разрешение на передачу данных не установлено")
        prompt = messages(rubric, conversation)
        if mode == "baseline":
            prompt[0]["content"] = BASELINE_SYSTEM
        payload = {"model": self.model, "messages": prompt, "format": output_schema(rubric, conversation), "stream": False,
                   "options": {"temperature": 0, "seed": 42, "num_predict": 8192, "num_ctx": 32768}, "keep_alive": "5m"}
        request = urllib.request.Request(self.endpoint + "/api/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        start = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    data = read_json(response)
                break
            except urllib.error.HTTPError as exc:
                if attempts < 2 and exc.code in (429, 503):
                    time.sleep(0.25)
                    continue
                raise ProviderError(f"Локальный провайдер: HTTP {exc.code}; результат не сохранён") from None
            except (OSError, TimeoutError):
                raise ProviderError("Соединение с моделью прервано или превышен таймаут; результат не сохранён") from None
        if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
            raise ProviderError("Провайдер вернул неверную структуру ответа")
        if data.get("done_reason") == "length" or data.get("done") is not True:
            raise ProviderError("Генерация модели не завершена; частичный результат отклонён")
        try:
            output = json.loads(data["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError):
            raise ProviderError("Модель не вернула валидный JSON по рубрике; оценка не сохранена") from None
        return materialize_evidence(output, conversation), {"provider": self.name, "model": self.model, "model_digest": selected["digest"], "evidence_mode": "SOURCE_TURN_LOOKUP",
                        "is_mock": False, "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                        "attempts": attempts, "input_tokens": data.get("prompt_eval_count"),
                        "output_tokens": data.get("eval_count"), "api_cost_usd": 0,
                        "cost_note": "Локальный вызов; стоимость оборудования и электроэнергии не измерена",
                        "parameters": payload["options"], "mode": mode, "request_hash": digest(payload)}


class MockProvider:
    """Deterministic fixture. Does not establish model accuracy or integration."""
    name = "mock"
    is_mock = True

    def evaluate(self, rubric, conversation, mode="evidence"):
        turns = conversation["transcript"]
        operator = next((t for t in turns if t["speaker"] == "operator"), None)
        rows = []
        for c in rubric["criteria"]:
            turn = operator or turns[0]
            answer = "NO" if c["kind"] == "flag" or not operator else "YES"
            na_reason = ""
            if c["na_allowed"]:
                answer = "N/A"
                na_reason = "Тестовое N/A для проверки перераспределения. Это заданная фикстура, а не вывод модели."
            elif c["kind"] == "numeric":
                answer = str(max(x["score"] for x in c["levels"])) if operator else "0"
            rows.append({"criterion_id": c["id"], "answer": answer,
                         "rationale": "MOCK: условный ответ для проверки интерфейса, хранения и расчётов. Не использовать как AI-оценку.",
                         "na_reason": na_reason, "ambiguous": True,
                         "limitations": ["Тестовая фикстура; семантика диалога не оценивалась"],
                         "coverage": {"start": turns[0]["id"], "end": turns[-1]["id"]},
                         "evidence": [{"turn_id": turn["id"], "speaker": turn["speaker"], "quote": turn["text"][:150]}]})
        return {"criteria": rows}, {"provider": "mock", "model": "contract-fixture-v1", "is_mock": True,
                                   "latency_ms": 0, "attempts": 0, "api_cost_usd": None, "mode": mode,
                                   "parameters": {}, "input_tokens": None, "output_tokens": None}


class ChatProvider:
    """Groq Cloud or local llama.cpp. Never sends data to OpenAI's API."""
    is_mock = False

    def __init__(self, provider, model, key="", free_plan_confirmed=False, endpoint="http://127.0.0.1:8081", timeout=150, opener=None):
        if provider not in ("groq", "llamacpp"):
            raise ValidationError("Неизвестный провайдер")
        self.name = provider
        self.model = model
        self.key = key
        self.free_plan_confirmed = free_plan_confirmed
        self.endpoint = "https://api.groq.com/openai/v1" if provider == "groq" else local_endpoint(endpoint) + "/v1"
        self.timeout = timeout
        self.opener = opener or safe_open

    def models(self):
        if self.name == "groq":
            return [{"name": "openai/gpt-oss-120b"}, {"name": "qwen/qwen3.8-27b"}, {"name": "openai/gpt-oss-20b"}]
        try:
            request=urllib.request.Request(self.endpoint+'/models',headers={'Authorization':'Bearer '+self.key} if self.key else {})
            with self.opener(request, timeout=3) as response:
                data = read_json(response)
            return [{"name": x["id"]} for x in data.get("data", [])]
        except (OSError, ProviderError, KeyError, TypeError):
            return []

    def evaluate(self, rubric, conversation, mode="evidence"):
        started = time.perf_counter()
        deadline = time.monotonic() + self.timeout
        context = {"http_calls": 0, "trace": [], "rejections": []}
        feedback = ""
        for repair in range(2):
            try:
                result, meta = self._evaluate_pass(rubric, conversation, mode, deadline, context, feedback)
                meta.update({"latency_ms": round((time.perf_counter()-started)*1000, 2),
                             "attempts": context["http_calls"], "validation_repairs": repair,
                             "attempt_trace": context["trace"], "rejected_responses": context["rejections"]})
                for field in ("input_tokens", "output_tokens"):
                    counts = [t.get(field) for t in context["trace"] if t.get("status") == "response"]
                    meta[field] = sum(counts) if counts and all(v is not None for v in counts) else None
                meta["token_usage_complete"] = all(meta.get(f) is not None for f in ("input_tokens", "output_tokens")) and not any(t.get("status") != "response" and t.get("http_status") != 429 for t in context["trace"])
                meta["retry_policy"] = "one_validator_repair; max_6_http; shared_deadline; reset_margin_0.5s"
                return result, meta
            except ProviderError as exc:
                if exc.code == "invalid_response":
                    context["rejections"].append({"error": str(exc), "metadata": dict(exc.metadata), "raw_response": exc.raw_response})
                exc.metadata.update({"provider": self.name, "model": self.model, "mode": mode, "is_mock": False,
                                     "latency_ms": round((time.perf_counter()-started)*1000, 2),
                                     "attempts": context["http_calls"], "attempt_trace": context["trace"],
                                     "rejected_responses": context["rejections"]})
                if exc.code != "invalid_response" or repair or deadline-time.monotonic() <= 1:
                    raise
                # Only validator feedback is appended; no invented/corrected grades.
                feedback = str(exc)[:1000]
        raise AssertionError("Unreachable")

    def _evaluate_pass(self, rubric, conversation, mode, deadline, context, feedback):
        if self.name != "groq" or (len(conversation["transcript"]) <= COMPACT_AFTER_TURNS and not self.model.startswith("openai/")):
            return self._evaluate_once(rubric, conversation, mode, timeout=max(0, deadline-time.monotonic()), context=context, feedback=feedback)
        started = time.perf_counter()
        parts, metadata = [], []
        for offset in range(0, len(rubric["criteria"]), 4):
            partial_rubric = {**rubric, "criteria": rubric["criteria"][offset:offset+4]}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("Общий таймаут длинного диалога исчерпан; частичная оценка отклонена")
            result, meta = self._evaluate_once(partial_rubric, conversation, mode, timeout=remaining, context=context, feedback=feedback)
            parts.extend(result["criteria"])
            metadata.append(meta)
        if len({m["model"] for m in metadata}) != 1:
            raise ProviderError("Модель изменилась между частями; частичная оценка отклонена")
        combined = dict(metadata[-1])
        for field in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
            values = [m.get(field) for m in metadata]
            combined[field] = sum(values) if all(v is not None for v in values) else None
        combined.update({"latency_ms": round((time.perf_counter() - started)*1000, 2),
                         "attempts": sum(m["attempts"] for m in metadata),
                         "request_hash": digest([m["request_hash"] for m in metadata]),
                         "criterion_batches": [{"criterion_ids": [c["id"] for c in rubric["criteria"][i*4:(i+1)*4]],
                                                "request_hash": m["request_hash"], "input_tokens": m.get("input_tokens"),
                                                "output_tokens": m.get("output_tokens"), "latency_ms": m["latency_ms"]}
                                               for i, m in enumerate(metadata)],
                         "full_transcript_each_batch": True})
        return {"criteria": parts}, combined

    def _evaluate_once(self, rubric, conversation, mode="evidence", timeout=None, context=None, feedback=""):
        if self.name == "groq" and (not self.key or not self.free_plan_confirmed):
            raise ProviderError("Для Groq нужны ключ и подтверждённый Free Plan в настройках. Платный доступ не используется")
        if self.name == "groq" and self.model not in {x["name"] for x in self.models()}:
            raise ProviderError("Модель не входит в проверенный список кандидатов Free Plan")
        prompt = messages(rubric, conversation)
        if mode == "baseline":
            prompt[0]["content"] = BASELINE_SYSTEM
        if feedback:
            prompt.append({"role": "user", "content": "Предыдущий ответ отклонён серверным валидатором: " + feedback + ". Повторно оцени исходный транскрипт по той же рубрике. Проверь шкалы, роли, ID и coverage. Не меняй оценки ради прохождения проверки: каждое решение должно соответствовать исходнику."})
        payload = {"model": self.model, "messages": prompt, "temperature": 0, "seed": 42,
                   "response_format": {"type": "json_schema", "json_schema": {"name": "cqa_evaluation", "strict": True, "schema": output_schema(rubric, conversation)}},
                   "stream": False}
        if self.name == "groq":
            payload["max_completion_tokens"] = 1280 if len(conversation["transcript"]) > COMPACT_AFTER_TURNS else 2048 if self.model.startswith("qwen/") or len(rubric["criteria"])<=4 else 4096
            if self.model.startswith("openai/"):
                payload["reasoning_effort"] = "low" if len(conversation["transcript"]) > COMPACT_AFTER_TURNS or len(rubric["criteria"])<=4 else "medium"
        else:
            payload["max_tokens"] = 6144
            payload["cache_prompt"] = True
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "CQA-Evidence/0.1"}
        if self.name == "groq":
            headers["Authorization"] = "Bearer " + self.key
        elif self.name=='llamacpp' and self.key:
            headers['Authorization']='Bearer '+self.key
        request = urllib.request.Request(self.endpoint + "/chat/completions", data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), headers=headers, method="POST")
        start = time.perf_counter()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        for attempt in range(1, 5):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError("Общий таймаут оценки с учётом повторов исчерпан")
                if context is not None:
                    if context["http_calls"] >= 6:
                        raise ProviderError("Достигнут предел 6 HTTP-попыток на диалог", code="attempt_budget")
                    context["http_calls"] += 1
                with self.opener(request, timeout=remaining) as response:
                    data = read_json(response)
                    limits = rate_headers(response)
                break
            except urllib.error.HTTPError as exc:
                limits = rate_headers(exc)
                try:
                    delay = float(exc.headers.get("retry-after", "2"))
                    if not math.isfinite(delay) or delay < 0: delay = 2
                except (ValueError, TypeError):
                    delay = 2
                details = {}
                if exc.code == 400:
                    try:
                        error = read_json(exc).get("error", {})
                        if isinstance(error, dict):
                            # Do not echo arbitrary provider text or request data in the UI.
                            details = {k: error[k] for k in ("code", "type", "failed_generation") if k in error}
                    except (ProviderError, OSError, AttributeError):
                        pass
                if context is not None:
                    context["trace"].append({"status": "http_error", "http_status": exc.code, "request_hash": digest(payload),
                                             "rate_limit_headers": limits, "retry_after": delay if exc.code in (429,503) else None})
                if exc.code == 400 and details.get("code") == "json_validate_failed":
                    raise ProviderError("Провайдер отклонил сгенерированный ответ как не соответствующий JSON-схеме", code="invalid_response",
                                        metadata={"request_hash": digest(payload)}, raw_response=details) from None
                if exc.code in (429, 503) and attempt < 4:
                    if delay > 55 or delay >= deadline - time.monotonic():
                        message = "Ограничение частоты API (429)" if exc.code == 429 else "Провайдер временно недоступен (503)"
                        raise ProviderError(message + ": длительное ожидание. Повторите позже; тип квоты уточните в аккаунте", code="rate_limit" if exc.code == 429 else "unavailable", retry_after=delay) from None
                    time.sleep(max(0.5, delay + 0.5))  # Allow for reset-boundary/network-clock skew.
                    continue
                raise ProviderError(f"{self.name}: HTTP {exc.code}. Проверьте ключ, доступ модели, размер контекста и квоты. Оценка не создана", code="rate_limit" if exc.code == 429 else "unavailable" if exc.code == 503 else "http_error", retry_after=delay if exc.code in (429,503) else None) from None
            except (OSError, TimeoutError):
                raise ProviderError(f"{self.name}: сеть или таймаут. Успешная оценка не создана") from None
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        if context is not None:
            context["trace"].append({"status": "response", "request_hash": digest(payload), "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")})
        content = None
        try:
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ProviderError("Ответ модели не завершён (лимит или отказ); частичная оценка отклонена")
            if choice["message"].get("refusal"):
                raise ProviderError("Модель отказалась оценивать диалог")
            content = choice["message"]["content"]
            output = decode_model_json(content)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("Модель вернула неверный JSON, структуру или повторяющийся ключ", code="invalid_response",
                                metadata={"request_hash": digest(payload)}, raw_response={"unparsed_content": content[:100000] if isinstance(content,str) else None}) from None
        metadata = {"provider": self.name, "model": data.get("model", self.model), "requested_model": self.model, "model_digest": None, "evidence_mode": "SOURCE_TURN_LOOKUP",
                        "is_mock": False, "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                        "attempts": attempt, "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                        "cached_input_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens"),
                        "reasoning_tokens": usage.get("completion_tokens_details", {}).get("reasoning_tokens"),
                        "api_cost_usd": 0, "cost_note": "Free Plan подтверждён пользователем; фактический тариф аккаунта через API не проверяется" if self.name == "groq" else "Локальный вызов; электроэнергия и оборудование не измерены",
                        "mode": mode, "parameters": {k: v for k, v in payload.items() if k not in ("messages", "response_format")},
                        "request_hash": digest(payload), "rate_limit_headers": limits}
        try:
            materialized = materialize_evidence(output, conversation)
            validate_decisions(materialized, rubric, conversation)
        except ValidationError as exc:
            raise ProviderError(str(exc), code="invalid_response", metadata=metadata, raw_response=output) from None
        return materialized, metadata
