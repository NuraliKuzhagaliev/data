"""Access-controlled, isolated, ephemeral judging workspaces for a single free host.

No organizer input or local credentials are copied into a hosted workspace.
Provider secrets come from host environment; visitors cannot change them.
"""
import contextlib
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .domain import ValidationError, records_from_payload, text
from .server import Application, ROOT, create_server, validate_http_origin
from .store import source_hash

MODELS = {"gemini": {"gemini-3.5-flash"},
          "groq": {"openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"}}
COOKIE = "cqa_session"


class HostedApplication(Application):
    def __init__(self, directory, registry):
        super().__init__(data_dir=directory)
        self.registry = registry
        self.running = registry.running
        self.config.update(provider=os.environ.get("CQA_PROVIDER", "gemini"),
                           model=os.environ.get("CQA_MODEL", "gemini-3.5-flash"),
                           team_name="THE BOYS")

    def public_config(self):
        return super().public_config() | {"hosted": True, "credentials_managed": True,
                                          "ephemeral_storage": True}

    def save_config(self, raw):
        if raw.get("key") or raw.get("gemini_key"):
            raise ValidationError("Ключи облачного демо задаются командой в настройках хостинга")
        provider = raw.get("provider", self.config["provider"])
        model = raw.get("model", self.config["model"])
        if model not in MODELS.get(provider, set()):
            raise ValidationError("В демо выберите проверенную модель Gemini или Groq")
        self.config.update(provider=provider, model=model,
                           team_name=text(raw.get("team_name", "THE BOYS"), "Команда", 200))
        return self.public_config()  # Keys remain in memory, never written to a session config file.

    def provider(self, kind, model):
        if kind == "mock":
            return super().provider(kind, model)
        if kind not in MODELS or (model and model not in MODELS[kind]):
            raise PermissionError("В облачном демо разрешены только проверенные облачные модели")
        return super().provider(kind, model)

    def models(self, kind):
        if kind not in MODELS:
            raise PermissionError("Локальные провайдеры в облачном демо недоступны")
        return [{"name": name, "remote": False} for name in sorted(MODELS[kind])]

    def start_job(self, raw, **kwargs):
        ids = raw.get("conversation_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 10:
            raise ValidationError("В одном демо-пакете допускается от 1 до 10 диалогов")
        carried = kwargs.get("resume_from") or {}
        needed = len(ids) - sum(r.get("status") == "ok" for r in carried.get("outcomes", []))
        with self.registry.lock:
            self.registry.reset_budget()
            if raw.get("provider", self.config["provider"]) != "mock" and self.registry.used + needed > self.registry.daily_limit:
                raise ValidationError("Дневной лимит AI-демо исчерпан. Сохраните данные и используйте локальную версию")
            result = super().start_job(raw, **kwargs)
            if not result["is_mock"]:
                self.registry.used += needed
            return result


class SessionRegistry:
    def __init__(self, directory, *, ttl=4*3600, maximum=16, daily_limit=20, seed=True):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ttl, self.maximum, self.daily_limit, self.seed = ttl, maximum, daily_limit, seed
        self.lock = threading.RLock()
        self.running = threading.BoundedSemaphore(1)
        self.sessions = {}
        self.used, self.budget_day = 0, None
        self.failures = {}

    def reset_budget(self):
        day = datetime.now(timezone.utc).date()
        if day != self.budget_day:
            self.used, self.budget_day = 0, day

    def get(self, token):
        with self.lock:
            entry = self.sessions.get(token)
            if entry and entry["expires"] > time.monotonic():
                entry["expires"] = time.monotonic() + self.ttl
                return entry["app"]
            return None

    def create(self):
        with self.lock:
            for token, entry in list(self.sessions.items()):
                if entry["expires"] <= time.monotonic() and not any(j["status"] == "running" for j in entry["app"].jobs.values()):
                    entry["app"].store.close()
                    del self.sessions[token]
            if len(self.sessions) >= self.maximum:
                raise ValidationError("Все демо-сеансы заняты. Попробуйте позднее или запустите локальную версию")
            token = secrets.token_urlsafe(32)
            app = HostedApplication(self.directory / token, self)
            if self.seed:
                examples = json.loads((ROOT / "examples/judge-demo.json").read_text(encoding="utf-8"))
                app.store.import_conversations(records_from_payload(examples))
            self.sessions[token] = {"app": app, "expires": time.monotonic() + self.ttl}
            return token, app

    def login_allowed(self, address):
        with self.lock:
            cutoff = time.monotonic() - 60
            self.failures = {ip: [t for t in times if t > cutoff] for ip, times in self.failures.items() if any(t > cutoff for t in times)}
            return len(self.failures.get(address, [])) < 8 and sum(map(len, self.failures.values())) < 256

    def failed_login(self, address):
        with self.lock:
            self.failures.setdefault(address, []).append(time.monotonic())

    def revoke(self, token):
        with self.lock:
            if token in self.sessions:
                self.sessions[token]["expires"] = 0

    def close(self):
        with self.lock:
            for entry in self.sessions.values():
                entry["app"].store.close()


class RequestApplication:
    """Resolve the existing application API to the current request's workspace."""
    def __init__(self):
        self.current = threading.local()

    @contextlib.contextmanager
    def use(self, app):
        self.current.app = app
        try:
            yield
        finally:
            del self.current.app

    def __getattr__(self, name):
        return getattr(self.current.app, name)


def create_hosted_server(directory, access_hash, *, origin, host="127.0.0.1", port=0, seed=True, daily_limit=20):
    parsed = urlsplit(origin)
    if not re.fullmatch(r"[0-9a-f]{64}", access_hash or ""):
        raise ValidationError("Для облачного демо нужен SHA256 кода доступа CQA_DEMO_ACCESS_HASH")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or not parsed.hostname:
        raise ValidationError("CQA_PUBLIC_ORIGIN должен содержать только HTTPS origin")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
        raise ValidationError("Публичный origin должен использовать HTTPS")
    origin = origin.rstrip("/")
    registry = SessionRegistry(directory, seed=seed, daily_limit=daily_limit)
    proxy = RequestApplication()
    server = create_server(proxy, host=host, port=port, public_origin=origin)
    base = server.RequestHandlerClass
    secure = parsed.scheme == "https"

    class HostedHandler(base):
        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def token(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                value = cookie.get(COOKIE)
                return value.value if value else ""
            except CookieError:
                return ""

        def redirect(self, path, cookie=None):
            self.send_response(303)
            self.send_header("Location", path)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def reject_large_body(self, size, message, maximum_drain=8_000_000):
            # Drain ordinary uploads before closing; an unread TCP body can reset
            # the connection and hide the useful 413 response from the client.
            remaining = min(size, maximum_drain)
            while remaining:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            return self.send({"error": message}, 413)

        def do_GET(self):
            try:
                validate_http_origin(self.headers, self.server.server_port, origin)
                path = urlsplit(self.path).path
                if path == "/api/health":
                    return self.send({"status": "ok", "service": "CQA Evidence", "hosted": True,
                                      "revision": os.environ.get("RENDER_GIT_COMMIT", "local-test"), "code_hash": source_hash()})
                if path == "/styles.css":
                    return self.send((ROOT / "web/styles.css").read_text(encoding="utf-8"), content_type="text/css; charset=utf-8")
                app = registry.get(self.token())
                if path == "/login" or (path == "/" and not app):
                    message = "Неверный код доступа" if parse_qs(urlsplit(self.path).query).get("error") else ""
                    page = (ROOT / "web/login.html").read_text(encoding="utf-8").replace("__LOGIN_ERROR__", html.escape(message))
                    return self.send(page, content_type="text/html; charset=utf-8")
                if not app:
                    return self.send({"error": "Сеанс демо завершён. Войдите заново через главную страницу"}, 401)
                with proxy.use(app):
                    return super().do_GET()
            except PermissionError as exc:
                self.send({"error": str(exc)}, 403)

        def do_POST(self):
            try:
                validate_http_origin(self.headers, self.server.server_port, origin)
                path = urlsplit(self.path).path
                size = int(self.headers.get("Content-Length", "0"))
                if path == "/auth/login":
                    if not registry.login_allowed(self.client_address[0]):
                        return self.send({"error": "Слишком много попыток входа. Повторите через минуту"}, 429)
                    if not 0 < size <= 2048:
                        return self.reject_large_body(max(0, size), "Неверный размер формы входа", 8192)
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded":
                        return self.send({"error": "Ожидается форма входа"}, 400)
                    form = parse_qs(self.rfile.read(size).decode("utf-8"))
                    supplied = hashlib.sha256(form.get("access_code", [""])[0].strip().encode()).hexdigest()
                    if not hmac.compare_digest(supplied, access_hash):
                        registry.failed_login(self.client_address[0])
                        return self.redirect("/login?error=1")
                    token, _ = registry.create()
                    value = f"{COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={registry.ttl}"
                    return self.redirect("/", value + ("; Secure" if secure else ""))
                app = registry.get(self.token())
                if not app:
                    return self.send({"error": "Для работы с демо нужен код доступа команды"}, 401)
                if size > 2_000_000:
                    return self.reject_large_body(size, "Демо принимает до 2 МБ за загрузку")
                if path == "/api/import-dev":
                    return self.send({"error": "Приватный DEV не размещается на публичном демо. Загрузите разрешённый собственный пример"}, 400)
                if path == "/auth/logout":
                    if self.headers.get("X-CQA-Token") != app.csrf:
                        raise PermissionError("Неверный токен сеанса")
                    registry.revoke(self.token())
                    return self.redirect("/login", f"{COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0" + ("; Secure" if secure else ""))
                with proxy.use(app):
                    return super().do_POST()
            except (ValueError, UnicodeDecodeError) as exc:
                self.send({"error": str(exc) if isinstance(exc, ValidationError) else "Некорректная форма"}, 400)
            except PermissionError as exc:
                self.send({"error": str(exc)}, 403)

    server.RequestHandlerClass = HostedHandler
    server.registry = registry
    return server


def serve_hosted():
    origin = os.environ.get("CQA_PUBLIC_ORIGIN") or os.environ.get("RENDER_EXTERNAL_URL", "")
    if not origin.startswith("https://"):
        raise ValidationError("Для host задайте HTTPS CQA_PUBLIC_ORIGIN или RENDER_EXTERNAL_URL")
    server = create_hosted_server(os.environ.get("CQA_HOSTED_DATA_DIR", str(ROOT / ".local/cloud-sessions")),
                                  os.environ.get("CQA_DEMO_ACCESS_HASH", ""), origin=origin,
                                  host="0.0.0.0", port=int(os.environ.get("PORT", "10000")),
                                  daily_limit=int(os.environ.get("CQA_DEMO_DAILY_LIMIT", "20")))
    def stop_on_signal(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_on_signal)
    print("CQA hosted demo ready; isolated workspaces, ephemeral storage, no HTTP body logging", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.registry.close()
    return 0
