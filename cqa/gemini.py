"""Gemini Developer API. Free-Tier confirmation is mandatory; no paid fallback.

Native REST fields follow the public v1beta discovery schema captured in
.local/quality-v3/research/gemini-discovery.json on 2026-11-08.
"""
import json
import math
import re
import time
import urllib.error
import urllib.request

from .domain import ValidationError, digest, materialize_evidence, validate_decisions, decode_model_json
from .prompts import BASELINE_SYSTEM, messages, output_schema
from .providers import ProviderError, read_json, safe_open


def error_quota(exc):
    """Read Google's bounded structured retry details, never echo the body/key."""
    delay=2.0; daily=False; quotas=[]
    try:
        header=float(exc.headers.get('retry-after','2'))
        if math.isfinite(header) and header>=0:delay=header
    except (ValueError,TypeError,AttributeError):pass
    try:
        data=json.loads(exc.read(64000))
        for detail in data.get('error',{}).get('details',[]):
            if detail.get('@type','').endswith('/google.rpc.RetryInfo'):
                match=re.fullmatch(r'(\d+(?:\.\d+)?)s',str(detail.get('retryDelay','')))
                if match:
                    seconds=float(match[1])
                    if math.isfinite(seconds):delay=max(delay,seconds)
            if detail.get('@type','').endswith('/google.rpc.QuotaFailure'):
                for item in detail.get('violations',[]):
                    quota_id=str(item.get('quotaId',''))[:200]
                    daily=daily or 'PerDay' in quota_id
                    quotas.append({'quota_id':quota_id,'limit':str(item.get('quotaValue',''))[:40]})
    except (ValueError,TypeError,AttributeError,OSError):pass
    return delay,{'daily_quota':daily,'quotas':quotas}


class GeminiProvider:
    name = "gemini"
    is_mock = False
    allowed_models = ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash")

    def __init__(self, model="gemini-3.8-flash", key="", free_plan_confirmed=False, timeout=150, opener=None):
        self.model, self.key, self.free_plan_confirmed = model, key, free_plan_confirmed
        self.timeout, self.opener = timeout, opener or safe_open

    def models(self):
        return [{"name": model} for model in self.allowed_models]

    def evaluate(self, rubric, conversation, mode="evidence"):
        if not self.key or not self.free_plan_confirmed:
            raise ProviderError("Для Gemini нужны ключ и подтверждённый Free Tier без платного биллинга", code="credentials")
        if self.model not in self.allowed_models:
            raise ValidationError("Модель Gemini не входит в проверенный список Free Tier")
        prompt = messages(rubric, conversation)
        if mode == "baseline": prompt[0]["content"] = BASELINE_SYSTEM
        started = time.perf_counter(); deadline = time.monotonic()+self.timeout
        trace, rejected, feedback = [], [], ""
        input_counts, output_counts = [], []
        url = "https://generativelanguage.googleapis.com/v1beta/models/"+self.model+":generateContent"
        attempts = 0
        metadata = {"provider": self.name, "model": self.model, "requested_model": self.model, "is_mock": False, "mode": mode,
                    "api_cost_usd": 0, "cost_note": "Gemini Free Tier подтверждён пользователем; фактический тариф через API не проверяется",
                    "evidence_mode": "SOURCE_TURN_LOOKUP", "data_note": "На Free Tier Google может использовать содержимое для улучшения продуктов"}
        try:
            for repair in range(2):
                contents = [{"role": "user", "parts": [{"text": prompt[1]["content"]}]}]
                if feedback: contents.append({"role": "user", "parts": [{"text": "Ответ отклонён валидатором: "+feedback+". Оцени тот же источник заново по той же рубрике, проверь ID, coverage и шкалы."}]})
                generation = {"temperature": 0, "maxOutputTokens": 8192, "thinkingConfig": {"thinkingLevel": "MEDIUM"},
                              "responseFormat": {"text": {"mimeType": "APPLICATION_JSON", "schema": output_schema(rubric, conversation)}}}
                payload = {"systemInstruction": {"parts": [{"text": prompt[0]["content"]}]}, "contents": contents, "generationConfig": generation}
                request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
                                                 headers={"Content-Type": "application/json", "x-goog-api-key": self.key, "User-Agent": "CQA-Evidence/0.2"}, method="POST")
                request_hash = digest(payload)
                for retry in range(3):
                    remaining = deadline-time.monotonic()
                    if remaining <= 0 or attempts >= 6: raise ProviderError("Gemini: исчерпан общий бюджет времени или попыток", code="timeout")
                    attempts += 1
                    try:
                        with self.opener(request, timeout=remaining) as response: data = read_json(response)
                        break
                    except urllib.error.HTTPError as exc:
                        delay,quota = error_quota(exc)
                        trace.append({"status": "http_error", "http_status": exc.code, "request_hash": request_hash, "retry_after": delay,**quota})
                        if exc.code in (429, 503) and not quota['daily_quota'] and retry < 2 and delay <= 55 and delay+0.5 < deadline-time.monotonic():
                            time.sleep(delay+0.5); continue
                        reason='исчерпана дневная квота' if quota['daily_quota'] else f'HTTP {exc.code}'
                        raise ProviderError(f"Gemini: {reason}; успешная оценка не создана", code="quota" if exc.code==429 else "unavailable" if exc.code==503 else "http_error", retry_after=delay if exc.code in (429,503) else None,metadata=quota) from None
                    except (OSError, TimeoutError):
                        raise ProviderError("Gemini: сеть или таймаут; частичная оценка не сохранена", code="network") from None
                if not isinstance(data, dict): raise ProviderError("Gemini вернул неверную структуру", code="invalid_response")
                usage = data.get("usageMetadata", {})
                count = usage.get("promptTokenCount"); input_counts.append(count)
                total = usage.get("totalTokenCount")
                out = total-count if type(total) is int and type(count) is int else usage.get("candidatesTokenCount")
                output_counts.append(out)
                trace.append({"status": "response", "request_hash": request_hash, "input_tokens": count, "output_tokens": out})
                metadata.update({"model": data.get("modelVersion", self.model), "request_hash": request_hash,
                                 "parameters": {k:v for k,v in generation.items() if k!='responseFormat'},
                                 "reasoning_tokens": usage.get("thoughtsTokenCount"), "cached_input_tokens": usage.get("cachedContentTokenCount"),
                                 "visible_output_tokens": usage.get("candidatesTokenCount")})
                candidates = data.get("candidates", [])
                if not candidates: raise ProviderError("Gemini не вернул оценку: отказ или блокировка ответа", code="refusal")
                choice = candidates[0]
                if choice.get("finishReason") != "STOP": raise ProviderError("Gemini не завершил ответ: лимит или блокировка", code="incomplete")
                content = "".join(p.get("text", "") for p in choice.get("content", {}).get("parts", []) if not p.get("thought"))
                try:
                    raw = decode_model_json(content)
                    result = materialize_evidence(raw, conversation)
                    validate_decisions(result, rubric, conversation)
                except (ValueError, TypeError) as exc:
                    rejected.append({"error": str(exc)[:1000], "metadata": {"request_hash": request_hash}, "raw_response": content})
                    if repair: raise ProviderError("Gemini: повторный ответ не прошёл валидацию", code="invalid_response") from None
                    feedback = str(exc)[:1000]; continue
                metadata.update({"latency_ms": round((time.perf_counter()-started)*1000,2), "attempts": attempts,
                                 "input_tokens": sum(input_counts) if all(v is not None for v in input_counts) else None,
                                 "output_tokens": sum(output_counts) if all(v is not None for v in output_counts) else None,
                                 "validation_repairs": repair, "attempt_trace": trace, "rejected_responses": rejected,
                                 "token_usage_complete": all(v is not None for v in input_counts+output_counts) and not any(t["status"]=="http_error" and t["http_status"]!=429 for t in trace)})
                return result, metadata
        except ProviderError as exc:
            quota_details={k:exc.metadata[k] for k in ('daily_quota','quotas') if k in exc.metadata}
            exc.metadata.update({**metadata, "attempts": attempts, "latency_ms": round((time.perf_counter()-started)*1000,2), "attempt_trace": trace, "rejected_responses": rejected})
            exc.metadata.update(quota_details)
            raise
        raise ProviderError("Gemini: валидный ответ не получен", code="invalid_response")
