"""Loopback-only HTTP adapter for the existing authenticated Earn OS services.

The browser receives only an HttpOnly session cookie; the underlying AuthService
token is never exposed to JavaScript. This is a local application server, not
a production or remote-network deployment interface.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import unquote, urlsplit
import hmac
import json
import mimetypes
import os
import secrets
import sys
from typing import cast

from .auth import AccessDenied, AuthError, AuthService
from .auth_storage import AuthStorage, StorageError
from .config import ConfigurationError, load_settings
from .dashboard_metrics import DashboardMetrics
from .state_storage import StateStorage, StateStorageError
from .workspace_api import WorkspaceAPI
from .ai_api import AIWorkspaceAPI

SESSION_COOKIE = "ai_earn_session"
CSRF_COOKIE = "ai_earn_csrf"
MAX_BODY = 16 * 1024
SESSION_MAX_AGE = 30 * 60
DASHBOARD_ROOT = Path(__file__).resolve().parent.parent / "dashboard"


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_equal(left: str, right: str) -> bool:
    return bool(left and right and hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8")))


def _cookie_value(header: str, name: str) -> str:
    jar = SimpleCookie()
    try:
        jar.load(header or "")
        morsel = jar.get(name)
        return morsel.value if morsel else ""
    except Exception:
        return ""


class EarnOSHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, handler, auth, state, metrics, dashboard_root):
        self.auth_service = auth
        self.state_storage = state
        self.dashboard_metrics = metrics
        self.workspace_api = WorkspaceAPI(auth, state)
        self.ai_api = AIWorkspaceAPI(auth, state)
        self.dashboard_root = dashboard_root.resolve()
        super().__init__(address, handler)


class EarnOSHandler(BaseHTTPRequestHandler):
    server_version = "EarnOSLocal"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Avoid writing request paths, query strings, account identifiers or cookies.
        sys.stderr.write("Earn OS local HTTP request handled.\n")

    def _headers(self, status: int, content_type: str, length: int, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()

    def _send_json(self, status: int, data: dict, cookies=None):
        body = _json_bytes(data)
        self._headers(status, "application/json; charset=utf-8", len(body), cookies)
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str):
        self._send_json(status, {"ok": False, "data": None, "error": {"code": code, "message": message}})

    def _host_is_loopback(self) -> bool:
        host = self.headers.get("Host", "")
        if not host or any(c in host for c in "\r\n/@"):
            return False
        if host.startswith("[") and "]" in host:
            hostname = host[1:host.index("]")]
        elif host.count(":") == 1:
            hostname = host.rsplit(":", 1)[0]
        else:
            hostname = host
        return hostname.lower() in {"127.0.0.1", "localhost", "::1"}

    def _origin_is_same(self) -> bool:
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        return _safe_equal(origin, "http://" + host)

    def _csrf_ok(self) -> bool:
        cookie = _cookie_value(self.headers.get("Cookie", ""), CSRF_COOKIE)
        header = self.headers.get("X-CSRF-Token", "")
        return _safe_equal(cookie, header)

    def _body(self) -> dict:
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit() or int(raw_length) > MAX_BODY:
            raise ValueError("Invalid request body.")
        raw = self.rfile.read(int(raw_length))
        if not raw:
            raise ValueError("Invalid request body.")
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Invalid request body.")
        return parsed

    def _session(self) -> str:
        return _cookie_value(self.headers.get("Cookie", ""), SESSION_COOKIE)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            if not self._host_is_loopback():
                self._error(403, "host_rejected", "Localhost access only.")
                return
            if path == "/api/v1/csrf":
                token = secrets.token_urlsafe(32)
                self._send_json(200, {"ok": True, "data": {"csrf_token": token}, "error": None}, [
                    ("Set-Cookie", f"{CSRF_COOKIE}={token}; Path=/; SameSite=Strict")
                ])
                return
            if path == "/api/v1/me":
                token = self._session()
                try:
                    profile = self.server.auth_service.profile(token)
                    self._send_json(200, {"ok": True, "data": {"account": profile}, "error": None})
                except (AccessDenied, AuthError):
                    self._error(401, "authentication_required", "Sign in to continue.")
                return
            if path == "/api/v1/dashboard/metrics":
                token = self._session()
                try:
                    self.server.auth_service.profile(token)
                    metrics = self.server.dashboard_metrics.get(token)
                    self._send_json(200, {"ok": True, "data": {
                        "format": "ai-earn-dashboard-snapshot", "version": 1,
                        "exported_at": metrics.get("generated_at"), "metrics": metrics,
                    }, "error": None})
                except (AccessDenied, AuthError):
                    self._error(401, "authentication_required", "Sign in to continue.")
                except Exception:
                    self._error(503, "metrics_unavailable", "Dashboard metrics are temporarily unavailable.")
                return
            if path.startswith("/api/v1/ai/"):
                token = self._session()
                try:
                    self.server.auth_service.profile(token)
                except (AccessDenied, AuthError):
                    self._error(401, "authentication_required", "Sign in to continue.")
                    return
                status, result = self.server.ai_api.dispatch("GET", path, token, query=urlsplit(self.path).query)
                self._send_json(status, result)
                return
            if path.startswith("/api/v1/workspace/"):
                token = self._session()
                try:
                    self.server.auth_service.profile(token)
                except (AccessDenied, AuthError):
                    self._error(401, "authentication_required", "Sign in to continue.")
                    return
                status, result = self.server.workspace_api.dispatch("GET", path, token, query=urlsplit(self.path).query)
                self._send_json(status, result)
                return
            self._error(404, "not_found", "API route not found.")
            return
        self._serve_static(path)

    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            self._error(404, "not_found", "Route not found.")
            return
        if not self._host_is_loopback():
            self._error(403, "host_rejected", "Localhost access only.")
            return
        if not self._origin_is_same():
            self._error(403, "origin_rejected", "Cross-origin requests are not allowed.")
            return
        if not self._csrf_ok():
            self._error(403, "csrf_rejected", "Refresh the page and try again.")
            return
        try:
            payload = self._body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid_request", "A valid JSON object is required.")
            return

        if path == "/api/v1/auth/login":
            if set(payload) != {"username", "password"}:
                self._error(400, "invalid_request", "Username and password are required.")
                return
            try:
                token = self.server.auth_service.login(payload["username"], payload["password"])
                profile = self.server.auth_service.profile(token)
                self._send_json(200, {"ok": True, "data": {"account": profile}, "error": None}, [
                    ("Set-Cookie", f"{SESSION_COOKIE}={token}; HttpOnly; Path=/; SameSite=Strict; Max-Age={SESSION_MAX_AGE}")
                ])
            except AuthError:
                self._error(401, "invalid_credentials", "Invalid username or password, or sign-in temporarily limited.")
            except Exception:
                self._error(503, "authentication_unavailable", "Sign-in is temporarily unavailable.")
            return

        if path == "/api/v1/auth/logout":
            if payload:
                self._error(400, "invalid_request", "Logout does not accept a request body.")
                return
            token = self._session()
            try:
                if token:
                    self.server.auth_service.logout(token)
            except Exception:
                # Clear the browser cookie even if the local store is unavailable.
                pass
            self._send_json(200, {"ok": True, "data": {"signed_out": True}, "error": None}, [
                ("Set-Cookie", f"{SESSION_COOKIE}=; HttpOnly; Path=/; SameSite=Strict; Max-Age=0")
            ])
            return
        if path.startswith("/api/v1/ai/"):
            token = self._session()
            try:
                self.server.auth_service.profile(token)
            except (AccessDenied, AuthError):
                self._error(401, "authentication_required", "Sign in to continue.")
                return
            status, result = self.server.ai_api.dispatch("POST", path, token, payload,
                operation_key=self.headers.get("Idempotency-Key"))
            self._send_json(status, result)
            return
        if path.startswith("/api/v1/workspace/"):
            token = self._session()
            try:
                self.server.auth_service.profile(token)
            except (AccessDenied, AuthError):
                self._error(401, "authentication_required", "Sign in to continue.")
                return
            status, result = self.server.workspace_api.dispatch(
                "POST", path, token, payload,
                operation_key=self.headers.get("Idempotency-Key"),
            )
            self._send_json(status, result)
            return
        self._error(404, "not_found", "API route not found.")

    def do_PATCH(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/v1/workspace/"):
            self._error(404, "not_found", "Route not found.")
            return
        if not self._host_is_loopback():
            self._error(403, "host_rejected", "Localhost access only.")
            return
        if not self._origin_is_same():
            self._error(403, "origin_rejected", "Cross-origin requests are not allowed.")
            return
        if not self._csrf_ok():
            self._error(403, "csrf_rejected", "Refresh the page and try again.")
            return
        try:
            payload = self._body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid_request", "A valid JSON object is required.")
            return
        token = self._session()
        try:
            self.server.auth_service.profile(token)
        except (AccessDenied, AuthError):
            self._error(401, "authentication_required", "Sign in to continue.")
            return
        status, result = self.server.workspace_api.dispatch(
            "PATCH", path, token, payload,
            operation_key=self.headers.get("Idempotency-Key"),
        )
        self._send_json(status, result)

    def do_OPTIONS(self):
        self._error(405, "method_not_allowed", "Method not allowed.")

    def _serve_static(self, request_path: str):
        if not self._host_is_loopback():
            self._error(403, "host_rejected", "Localhost access only.")
            return
        decoded = unquote(request_path)
        relative = "index.html" if decoded in {"", "/"} else decoded.lstrip("/")
        server = cast(EarnOSHTTPServer, self.server)
        candidate = (server.dashboard_root / relative).resolve()
        try:
            candidate.relative_to(server.dashboard_root)
        except ValueError:
            self._error(404, "not_found", "File not found.")
            return
        if not candidate.is_file():
            self._error(404, "not_found", "File not found.")
            return
        content = candidate.read_bytes()
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            mime += "; charset=utf-8"
        self._headers(200, mime, len(content))
        self.wfile.write(content)


def run(environ=None, host="127.0.0.1", port=8765):
    """Run the local web app; existing DBs only, never initialize or migrate."""
    env = os.environ if environ is None else environ
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Earn OS may only bind to loopback.")
    settings = load_settings(env)
    if settings.environment == "production":
        raise ConfigurationError("AI_EARN_ENV")
    auth_path = env.get("AI_EARN_AUTH_DB", "")
    state_path = env.get("AI_EARN_STATE_DB", "")
    if not auth_path or not state_path:
        raise ConfigurationError("AI_EARN_AUTH_DB and AI_EARN_STATE_DB")
    try:
        auth = AuthService(AuthStorage(Path(auth_path), create=False))
        state = StateStorage(Path(state_path))
    except (StorageError, StateStorageError) as exc:
        raise ConfigurationError("AI_EARN_AUTH_DB or AI_EARN_STATE_DB") from exc
    metrics = DashboardMetrics(auth, state)
    server = EarnOSHTTPServer((host, port), EarnOSHandler, auth, state, metrics, DASHBOARD_ROOT)
    print(f"Earn OS is available at http://127.0.0.1:{server.server_port}/", flush=True)
    print("Loopback only. Existing databases only; no schema migration or account creation.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
