import copy
import json
import mimetypes
import os
import secrets
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .domain import ValidationError, records_from_payload, text
from .exporting import csv_export, submission
from .providers import ChatProvider, MockProvider, OllamaProvider, ProviderError
from .store import Store, now
from .configuration import dotenv_config, environment_config
from .prompts import prompt_hash

ROOT = Path(__file__).resolve().parent.parent


def normalize_gemini_key(value):
    if not isinstance(value, str):
        raise ValidationError("Вставьте значение API-ключа из Google AI Studio")
    token = value.strip()
    assignment = re.match(r"^(?:export\s+|\$env:)?(?:GEMINI_API_KEY|GOOGLE_API_KEY)\s*=\s*(.+)$", token, re.DOTALL)
    if assignment:
        token = assignment.group(1).strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"', '`'):
        token = token[1:-1].strip()
    if token.startswith(("gsk_", "sk-proj-", "sk-ant-")):
        raise ValidationError("Это ключ другого провайдера. Для Gemini вставьте ключ из Google AI Studio")
    if not 16 <= len(token) <= 1024 or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValidationError("Вставьте само значение ключа Gemini без команды, внутренних пробелов и переносов строки")
    return token


class Application:
    def __init__(self, db_path=None, data_dir=None, config_path=None):
        self.data_dir = Path(data_dir or ROOT / ".local")
        self.store = Store(db_path or self.data_dir / "cqa.sqlite3")
        self.csrf = secrets.token_urlsafe(32)
        self.jobs = {}
        self.job_lock = threading.RLock()
        self.running = threading.BoundedSemaphore(1)
        self.config_path = Path(config_path) if config_path else self.data_dir / "provider.json"
        self.config = {"provider": "groq", "model": "openai/gpt-oss-120b", "free_plan_confirmed": False,
                       "team_name": "", "key": "", "gemini_key": "", "gemini_free_plan_confirmed": False}
        env_path = ROOT / '.env' if self.config_path.resolve()==(ROOT/'.local/provider.json').resolve() else self.data_dir/'.env'
        self.config.update(dotenv_config(env_path))
        if self.config_path.exists():
            self.config.update(json.loads(self.config_path.read_text(encoding="utf-8")))
        self.config.update(environment_config(os.environ))

    def public_config(self):
        return {k: self.config[k] for k in ("provider", "model", "team_name", "free_plan_confirmed", "gemini_free_plan_confirmed")} | {"key_present": bool(self.config.get("key")), "gemini_key_present": bool(self.config.get("gemini_key"))}

    def save_config(self, raw):
        provider = raw.get("provider", self.config["provider"])
        if provider not in ("groq", "gemini", "llamacpp", "ollama"):
            raise ValidationError("Провайдер: groq, gemini, llamacpp, ollama")
        config = {**self.config, "provider": provider, "model": text(raw.get("model", self.config["model"]), "Модель", 200),
                  "team_name": text(raw.get("team_name", self.config.get("team_name", "")), "Команда", 200, empty=True)}
        if "free_plan_confirmed" in raw:
            if type(raw["free_plan_confirmed"]) is not bool:
                raise ValidationError("Подтверждение Free Plan должно быть boolean")
            config["free_plan_confirmed"] = raw["free_plan_confirmed"]
        if "gemini_free_plan_confirmed" in raw:
            if type(raw["gemini_free_plan_confirmed"]) is not bool:
                raise ValidationError("Подтверждение Gemini Free Tier должно быть boolean")
            config["gemini_free_plan_confirmed"] = raw["gemini_free_plan_confirmed"]
        gemini_key = raw.get("gemini_key", "")
        if gemini_key:
            config["gemini_key"] = normalize_gemini_key(gemini_key)
        key = raw.get("key", "")
        if key:
            if not isinstance(key, str) or not key.startswith("gsk_") or len(key) > 500:
                raise ValidationError("Ожидается ключ Groq, начинающийся с gsk_")
            config["key"] = key.strip()
        self.config_path.parent.mkdir(exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.config_path)
        self.config = config
        return self.public_config()

    def provider(self, kind, model):
        if kind == "mock":
            return MockProvider()
        if kind == "ollama":
            return OllamaProvider(model=model)
        if kind == "gemini":
            from .gemini import GeminiProvider
            return GeminiProvider(model, key=self.config.get("gemini_key", ""), free_plan_confirmed=self.config.get("gemini_free_plan_confirmed", False))
        if kind=='llamacpp':
            key_file=self.config_path.parent/'local-server.key'
            local_key=key_file.read_text(encoding='utf-8').strip() if key_file.exists() else ''
            return ChatProvider(kind,model,key=local_key)
        return ChatProvider(kind, model, key=self.config.get("key", ""), free_plan_confirmed=self.config.get("free_plan_confirmed", False))

    def provider_options(self):
        # Local9B failed the long-input acceptance test; do not offer it in the UI.
        options=[{'name':'groq','label':'Groq · Free Plan','available':True},
                 {'name':'gemini','label':'Gemini · Free Tier','available':True}]
        return {'options':options,'local_available':False}

    def resume_job(self, key, raw):
        parent = self.job(key)
        if parent["status"] == "running":
            raise ValidationError("Нельзя продолжать работающий пакет")
        ids = parent.get("conversation_ids")
        if not ids:
            if len(parent["outcomes"]) != parent["total"]:
                raise ValidationError("В старом пакете не сохранён план ID: выберите недостающие диалоги вручную")
            ids = [row["internal_id"] for row in parent["outcomes"]]
        carried = [row for row in parent["outcomes"] if row["status"] == "ok"]
        if len(carried) == len(ids):
            raise ValidationError("Все диалоги уже успешно оценены")
        if parent["is_mock"]:
            raise ValidationError("MOCK-пакет нельзя продолжить как реальный AI-анализ")
        return self.start_job({"conversation_ids": ids, "rubric_id": parent["rubric_id"],
                               "mode": parent.get("mode", "evidence"),
                               "provider": raw.get("provider", self.config["provider"]),
                               "model": raw.get("model", self.config["model"])}, resume_from=parent, verification_of=parent.get("recheck_of"))

    def recheck_evaluation(self, key, raw):
        original = self.store.evaluation(key)
        if original["provenance"].get("is_mock"):
            raise ValidationError("Для перепроверки выберите реальную AI-оценку")
        return self.start_job({"conversation_ids": [original["conversation_id"]], "rubric_id": original["rubric"]["id"],
                               "provider": raw.get("provider", self.config["provider"]), "model": raw.get("model", self.config["model"])},
                              verification_of=key)

    def start_job(self, raw, *, resume_from=None, verification_of=None):
        ids = raw.get("conversation_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
            raise ValidationError("Укажите уникальные ID диалогов")
        rubric = self.store.rubric(raw.get("rubric_id"))
        carried = copy.deepcopy([row for row in resume_from["outcomes"] if row["status"] == "ok"]) if resume_from else []
        successful_ids = {row["internal_id"] for row in carried}
        conversations = [self.store.conversation(key) for key in ids if key not in successful_ids]
        kind = raw.get("provider", self.config["provider"])
        model = raw.get("model", self.config["model"])
        if kind not in ("groq", "gemini", "llamacpp", "ollama", "mock"):
            raise ValidationError("Неизвестный провайдер")
        mode = raw.get("mode", "evidence")
        if mode not in ("baseline", "evidence"):
            raise ValidationError("Режим: baseline или evidence")
        provider = self.provider(kind, model)
        if not self.running.acquire(blocking=False):
            raise ValidationError("Уже идёт оценка. Дождитесь результата перед повторным запуском")
        key = "job-" + uuid.uuid4().hex
        job = {"id": key, "status": "running", "created_at": now(), "completed": len(carried), "total": len(ids),
               "conversation_ids": ids, "outcomes": carried, "provider": kind, "model": model, "rubric_id": rubric["id"],
               "mode": mode, "is_mock": kind == "mock", "code_hash": self.store.code_hash,
               "parent_job_id": resume_from["id"] if resume_from else None, "preserved_successes": len(carried)}
        job["recheck_of"] = verification_of
        with self.job_lock:
            self.jobs[key] = job
            try:
                self._save_job(job)  # Persist the complete plan before the first network call.
            except Exception:
                del self.jobs[key]
                self.running.release()
                raise

        def run():
            try:
                for conv in conversations:
                    try:
                        output, metadata = provider.evaluate(rubric, conv, mode=mode)
                        if verification_of:
                            metadata = {**metadata, "independent_recheck_of": verification_of,
                                        "recheck_method": "new_request_without_prior_answers; original_rubric"}
                        evaluation = self.store.save_evaluation(conv, rubric, output, metadata)
                        outcome = {"conversation_id": conv["conversation_id"], "internal_id": conv["id"], "status": "ok", "evaluation_id": evaluation["id"]}
                    except ProviderError as exc:
                        self.store.record_failure(conv, rubric, str(exc), {**exc.metadata, "provider": kind, "model": model,
                                                  "error_code": exc.code, "retry_after": exc.retry_after}, exc.raw_response)
                        outcome = {"conversation_id": conv["conversation_id"], "internal_id": conv["id"], "status": "error",
                                   "error": str(exc), "error_code": exc.code, "retry_after": exc.retry_after,
                                   "daily_quota": bool(exc.metadata.get('daily_quota'))}
                    except ValidationError as exc:
                        outcome = {"conversation_id": conv["conversation_id"], "internal_id": conv["id"], "status": "error", "error": str(exc)}
                    except Exception:
                        outcome = {"conversation_id": conv["conversation_id"], "internal_id": conv["id"], "status": "error", "error": "Внутренняя ошибка оценки; успешный результат не создан"}
                    with self.job_lock:
                        job["outcomes"].append(outcome)
                        job["completed"] += 1
                        self._save_job(job)
                    if outcome.get('error_code') in ('quota','rate_limit') and (outcome.get('daily_quota') or (outcome.get('retry_after') or 0)>55):
                        job['status']='interrupted'
                        job['stop_reason']='Лимит провайдера: оставшиеся диалоги сохранены для продолжения'
                        break
                with self.job_lock:
                    if job['status']=='running':job["status"] = "completed"
                    job["completed_at"] = now()
                    self._save_job(job)
            finally:
                self.running.release()
        threading.Thread(target=run, daemon=True).start()
        return self.job(key)

    def _save_job(self, job):
        target = self.data_dir / "jobs" / (job["id"] + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        temp.replace(target)

    def job(self, key):
        if not isinstance(key, str) or not key.startswith("job-") or len(key) != 36 or not all(x in "0123456789abcdef" for x in key[4:]):
            raise KeyError(key)
        with self.job_lock:
            if key in self.jobs:
                return copy.deepcopy(self.jobs[key])
            target = self.data_dir / "jobs" / (key + ".json")
            if target.exists():
                job = json.loads(target.read_text(encoding="utf-8"))
                if job["status"] == "running":
                    job["status"] = "interrupted"
                return job
        raise KeyError(key)

    def latest_job(self):
        with self.job_lock:
            keys = set(self.jobs) | {p.stem for p in (self.data_dir / "jobs").glob("job-*.json")}
            jobs = [self.job(key) for key in keys]
        return max(jobs, key=lambda j: j["created_at"]) if jobs else None

    def submission_for_job(self, key, dataset, team):
        job = self.job(key)
        if job["status"] not in ("completed", "interrupted") or job["is_mock"]:
            raise ValidationError("Дождитесь окончания реального AI-пакета; MOCK для сдачи запрещён")
        outcomes = [self.store.evaluation(row["evaluation_id"]) if row["status"] == "ok" else row for row in job["outcomes"]]
        if job["completed"] != job["total"]:
            raise ValidationError("Пакет прерван: повторите недостающие диалоги до выгрузки")
        return submission(team, outcomes, dataset, [x["conversation_id"] for x in job["outcomes"]])


def create_server(app, host="127.0.0.1", port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CQA/0.1"

        def log_message(self, *_):
            pass  # No transcript, credential or signed URL in HTTP logs.

        def send(self, data, status=200, content_type="application/json; charset=utf-8", filename=None):
            if data is None or isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            elif isinstance(data, str):
                data = data.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; media-src https:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def guard(self, mutation=False):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed:
                raise PermissionError("Недопустимый Host")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + x for x in allowed}:
                raise PermissionError("Запрос с другого сайта запрещён")
            if mutation and self.headers.get("X-CQA-Token") != app.csrf:
                raise PermissionError("Неверный токен локального приложения; обновите страницу")

        def do_GET(self):
            try:
                self.guard()
                parsed = urlsplit(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                if path == "/api/health":
                    return self.send({"status": "ok", "service": "CQA Evidence", "version": "0.1.0", "code_hash": app.store.code_hash})
                if path == "/api/config":
                    return self.send(app.public_config())
                if path=='/api/provider-options':
                    return self.send(app.provider_options())
                if path == "/api/latest-job":
                    return self.send(app.latest_job())
                if path == "/api/models":
                    kind = query.get("provider", [app.config["provider"]])[0]
                    return self.send({"provider": kind, "models": app.provider(kind, "").models()})
                if path == "/api/rubrics":
                    return self.send(app.store.rubrics())
                if path == "/api/conversations":
                    return self.send([{k: v for k, v in c.items() if k not in ("transcript", "audio_url")} | {"turn_count": len(c["transcript"])} for c in app.store.conversations()])
                if path.startswith("/api/conversations/"):
                    return self.send(app.store.conversation(path.rsplit("/", 1)[1]))
                if path == "/api/evaluations":
                    return self.send([{k: v for k, v in e.items() if k not in ("criteria", "rubric")} for e in app.store.evaluations()])
                if path.startswith("/api/evaluations/"):
                    key = path.split("/")[3]
                    evaluation = app.store.evaluation(key)
                    if path.endswith("/export"):
                        if query.get("format") == ["csv"]:
                            return self.send(csv_export(evaluation), content_type="text/csv; charset=utf-8", filename="cqa-evaluation.csv")
                        return self.send(evaluation, filename="cqa-evaluation.json")
                    return self.send(evaluation)
                if path.startswith("/api/jobs/"):
                    key = path.split("/")[3]
                    if path.endswith("/submission"):
                        dataset = query.get("dataset", ["dev"])[0]
                        team = query.get("team", [app.config.get("team_name", "")])[0]
                        return self.send(app.submission_for_job(key, dataset, team), filename="results.json")
                    return self.send(app.job(key))
                if path == "/":
                    html = (ROOT / "web/index.html").read_text(encoding="utf-8").replace("__CQA_TOKEN__", app.csrf)
                    return self.send(html, content_type="text/html; charset=utf-8")
                files = {"/app.js": "app.js", "/styles.css": "styles.css"}
                if path in files:
                    file = ROOT / "web" / files[path]
                    return self.send(file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] + "; charset=utf-8")
                return self.send({"error": "Не найдено"}, 404)
            except (ValidationError, TypeError, ValueError) as exc:
                self.send({"error": str(exc)}, 400)
            except KeyError:
                self.send({"error": "Запись не найдена"}, 404)
            except PermissionError as exc:
                self.send({"error": str(exc)}, 403)
            except Exception:
                self.send({"error": "Внутренняя ошибка сервера"}, 500)

        def do_POST(self):
            try:
                self.guard(mutation=True)
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValidationError("Ожидается Content-Type: application/json")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8_000_000:
                    return self.send({"error": "Размер запроса должен быть от 1 байта до 8 МБ"}, 413)
                raw = json.loads(self.rfile.read(size))
                path = urlsplit(self.path).path
                if path == "/api/import":
                    return self.send(app.store.import_conversations(records_from_payload(raw)), 201)
                if path == "/api/import-dev":
                    source = ROOT / ".local/dev.json"
                    if not source.exists():
                        raise ValidationError('DEV ещё не установлен. Импортируйте JSON организаторов через «Загрузить диалог»; исходные данные не входят в публичный репозиторий')
                    records = records_from_payload(json.loads(source.read_text(encoding="utf-8")))
                    return self.send(app.store.import_conversations(records), 201)
                if not isinstance(raw, dict):
                    raise ValidationError("Ожидается JSON-объект")
                if path == "/api/config":
                    return self.send(app.save_config(raw))
                if path == "/api/rubrics":
                    return self.send(app.store.save_rubric(raw), 201)
                if path == "/api/evaluate":
                    return self.send(app.start_job(raw), 202)
                if path.startswith("/api/evaluations/") and path.endswith("/recheck"):
                    return self.send(app.recheck_evaluation(path.split("/")[3], raw), 202)
                if path.startswith("/api/jobs/") and path.endswith("/resume"):
                    return self.send(app.resume_job(path.split("/")[3], raw), 202)
                if path == "/api/appeals":
                    return self.send(app.store.appeal(raw.get("evaluation_id"), raw.get("criterion_id"), raw.get("comment")), 201)
                if path.startswith("/api/appeals/") and path.endswith("/resolve"):
                    return self.send(app.store.resolve(path.split("/")[3], raw.get("decision"), raw.get("new_answer"), raw.get("reason"), raw.get("evidence_turns")))
                return self.send({"error": "Не найдено"}, 404)
            except (ValidationError, TypeError, ValueError) as exc:
                self.send({"error": str(exc)}, 400)
            except KeyError:
                self.send({"error": "Запись не найдена"}, 404)
            except PermissionError as exc:
                self.send({"error": str(exc)}, 403)
            except Exception:
                self.send({"error": "Внутренняя ошибка; успешный результат не создан"}, 500)

    return ThreadingHTTPServer((host, port), Handler)
