"""Auth helpers — bounded API key, no secrets in logs."""

from .base import AuthError as _AuthError
from .base import ConnectorError

import base64 as _base64
import hashlib as _hashlib
import hmac as _hmac
import http.server as _http_server
import json as _json
import os as _os
import secrets as _secrets
import socket as _socket
import time as _time
import urllib.parse as _urlparse
import urllib.request as _urlrequest
from typing import Callable as _Callable


def validate_api_key(value: bytes | str) -> bytes:
    # Accept bytes or str, 32..128 chars/bytes, no control/whitespace tricks, no secrets logged
    if isinstance(value, str):
        # str must be 32..128, printable, no control
        if not 32 <= len(value) <= 128:
            raise ConnectorError("Invalid API key length.")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ConnectorError("Invalid API key.")
        if any(c.isspace() for c in value):
            raise ConnectorError("Invalid API key.")
        return value.encode("utf-8")
    if isinstance(value, bytes):
        if not 32 <= len(value) <= 128:
            raise ConnectorError("Invalid API key length.")
        if any(b < 32 or b == 127 for b in value):
            raise ConnectorError("Invalid API key.")
        # no whitespace bytes
        if any(chr(b).isspace() for b in value):
            raise ConnectorError("Invalid API key.")
        return value
    raise ConnectorError("Invalid API key type.")


def redact_config(config: dict) -> dict:
    """Return copy with api_key redacted for logs."""
    if not isinstance(config, dict):
        return {}
    out = {}
    for k, v in config.items():
        if k.lower() in {"api_key", "apikey", "token", "secret"}:
            out[k] = "***redacted***"
        else:
            out[k] = v
    return out


# --- OAuth 2.0 authorization-code flow with PKCE (P2-2) ---------------------
# Fixed provider endpoints, bounded HTTPS, no redirects, no proxy-from-env,
# in-memory tokens only. Live use requires a disposable provider test account;
# the test suite mocks all transport.


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_DRIVE_FILES_ENDPOINT = "https://www.googleapis.com/drive/v3/files"
GOOGLE_GMAIL_MESSAGES_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GITHUB_API_BASE = "https://api.github.com"
GOOGLE_SCOPES = {
    "drive.file": "https://www.googleapis.com/auth/drive.file",
    "gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
}
_HTTP_RESPONSE_LIMIT = 65_536
_HTTP_TIMEOUT_SECONDS = 10


class _NoRedirectHandler(_urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener(request, *, timeout: int):
    return _urlrequest.build_opener(
        _urlrequest.ProxyHandler({}),
        _NoRedirectHandler(),
    ).open(request, timeout=timeout)


def _read_response(opener, request) -> bytes:
    try:
        response = opener(request, timeout=_HTTP_TIMEOUT_SECONDS)
        try:
            final_url = response.geturl() if hasattr(response, "geturl") else request.full_url
            if final_url != request.full_url:
                raise ConnectorError("OAuth provider redirected a credential-bearing request.")
            status = response.getcode()
            if type(status) is int and not 200 <= status < 300:
                raise ConnectorError("OAuth provider returned an unsuccessful response.")
            payload = response.read(_HTTP_RESPONSE_LIMIT + 1)
        finally:
            response.close()
    except ConnectorError:
        raise
    except Exception:
        raise ConnectorError("OAuth provider request failed.") from None
    if not isinstance(payload, bytes) or len(payload) > _HTTP_RESPONSE_LIMIT:
        raise ConnectorError("OAuth provider response exceeds the configured limit.")
    return payload


def _safe_json(payload: bytes) -> dict:
    try:
        value = _json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as exc:
        raise ConnectorError("OAuth provider returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise ConnectorError("OAuth provider returned an invalid response.")
    return value


def _validate_redirect_uri(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ConnectorError("Invalid OAuth redirect URI.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConnectorError("Invalid OAuth redirect URI.")
    try:
        parsed = _urlparse.urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ConnectorError("Invalid OAuth redirect URI.") from exc
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ConnectorError("OAuth redirect URI must use HTTPS or loopback HTTP.")
    if not host or parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ConnectorError("Invalid OAuth redirect URI.")
    if port is not None and not 1 <= port <= 65535:
        raise ConnectorError("Invalid OAuth redirect URI.")
    return value


class OAuthFlow:
    """Single-use Google OAuth authorization-code flow with PKCE."""

    def __init__(self, *, client_id: str, redirect_uri: str, scopes=("drive.file",), client_secret: str | None = None, opener: _Callable | None = None) -> None:
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 512 or any(ord(c) < 32 or ord(c) == 127 for c in client_id):
            raise ConnectorError("Invalid OAuth client ID.")
        if not isinstance(scopes, (list, tuple)) or not scopes or len(scopes) > 4:
            raise ConnectorError("Invalid OAuth scopes.")
        normalized_scopes = tuple(dict.fromkeys(scopes))
        if any(scope not in GOOGLE_SCOPES for scope in normalized_scopes):
            raise ConnectorError("OAuth scope is not allowlisted.")
        if client_secret is not None and (not isinstance(client_secret, str) or not 1 <= len(client_secret) <= 1024 or any(ord(c) < 32 or ord(c) == 127 for c in client_secret)):
            raise ConnectorError("Invalid OAuth client secret.")
        self.client_id = client_id
        self.redirect_uri = _validate_redirect_uri(redirect_uri)
        self.scopes = normalized_scopes
        self._client_secret = client_secret
        self._opener = opener or _default_opener
        self._pending: tuple[str, str] | None = None
        self._access_token: str | None = None
        self._token_expires_at: float | None = None
        self._revoked = False

    @classmethod
    def from_environment(cls, *, scopes=("drive.file",), opener: _Callable | None = None) -> "OAuthFlow":
        client_id = _os.environ.get("AI_EARN_GOOGLE_CLIENT_ID", "")
        redirect_uri = _os.environ.get("AI_EARN_GOOGLE_REDIRECT_URI", "")
        client_secret = _os.environ.get("AI_EARN_GOOGLE_CLIENT_SECRET")
        if not client_id or not redirect_uri:
            raise ConnectorError("Google OAuth client ID and redirect URI are required.")
        return cls(client_id=client_id, redirect_uri=redirect_uri, scopes=scopes, client_secret=client_secret, opener=opener)

    @property
    def connected(self) -> bool:
        return self._access_token is not None and self._token_expires_at is not None and _time.monotonic() < self._token_expires_at and not self._revoked

    @property
    def revoked(self) -> bool:
        return self._revoked

    def begin_authorization(self) -> str:
        self._access_token = None
        self._token_expires_at = None
        verifier = _secrets.token_urlsafe(64)
        state = _secrets.token_urlsafe(32)
        challenge = _base64.urlsafe_b64encode(_hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        self._pending = (state, verifier)
        self._revoked = False
        query = _urlparse.urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES[scope] for scope in self.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"

    def wait_for_local_callback(self, *, timeout: int = 180) -> tuple[str, str]:
        """Wait on an explicitly configured loopback redirect URI for one callback."""
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ConnectorError("Invalid OAuth callback timeout.")
        if self._pending is None:
            raise _AuthError("Start OAuth authorization before waiting for a callback.")
        parsed = _urlparse.urlsplit(self.redirect_uri)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "http" or host not in {"localhost", "127.0.0.1", "::1"}:
            raise ConnectorError("Local OAuth callback requires an HTTP loopback redirect URI.")
        callback_path = parsed.path or "/"
        expected_host = parsed.netloc.lower()
        expected_state = self._pending[0]
        result: dict[str, str] = {}

        class CallbackHandler(_http_server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("Host", "").lower() != expected_host:
                    self.send_error(400)
                    return
                callback = _urlparse.urlsplit(self.path)
                if callback.path != callback_path:
                    self.send_error(404)
                    return
                try:
                    params = _urlparse.parse_qs(callback.query, max_num_fields=8, strict_parsing=True)
                except ValueError:
                    self.send_error(400)
                    return
                states = params.get("state", [])
                codes = params.get("code", [])
                errors = params.get("error", [])
                if len(states) != 1 or not states[0].isascii() or not _hmac.compare_digest(states[0], expected_state):
                    result["invalid_state"] = "1"
                    self.send_error(400)
                    return
                if len(errors) == 1:
                    result["error"] = "authorization_denied"
                elif len(codes) == 1 and 1 <= len(codes[0]) <= 4096 and not any(ord(c) < 32 or ord(c) == 127 for c in codes[0]):
                    result["code"] = codes[0]
                    result["state"] = states[0]
                else:
                    self.send_error(400)
                    return
                body = b"Authorization response received. You may close this window."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                return

        server_class = _http_server.HTTPServer
        if ":" in host:
            class IPv6HTTPServer(_http_server.HTTPServer):
                address_family = _socket.AF_INET6

            server_class = IPv6HTTPServer
        try:
            server = server_class((host, parsed.port or 80), CallbackHandler)
        except OSError:
            raise ConnectorError("Unable to bind the configured local OAuth callback.") from None
        deadline = _time.monotonic() + timeout
        try:
            while not result and _time.monotonic() < deadline:
                server.timeout = min(1.0, max(0.01, deadline - _time.monotonic()))
                server.handle_request()
        finally:
            server.server_close()
        if "invalid_state" in result:
            self._pending = None
            raise _AuthError("OAuth state validation failed.")
        if "error" in result:
            self._pending = None
            raise _AuthError("OAuth authorization was denied.")
        if "code" not in result:
            raise _AuthError("OAuth callback timed out.")
        return result["code"], result["state"]

    def exchange_code(self, *, code: str, state: str) -> None:
        pending, self._pending = self._pending, None
        if pending is None or not isinstance(state, str) or not 1 <= len(state) <= 256 or not state.isascii() or not _hmac.compare_digest(state, pending[0]):
            raise _AuthError("OAuth state validation failed.")
        if not isinstance(code, str) or not 1 <= len(code) <= 4096 or any(ord(c) < 32 or ord(c) == 127 for c in code):
            raise _AuthError("Invalid OAuth authorization code.")
        form = {
            "client_id": self.client_id,
            "code": code,
            "code_verifier": pending[1],
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }
        if self._client_secret is not None:
            form["client_secret"] = self._client_secret
        request = _urlrequest.Request(
            GOOGLE_TOKEN_ENDPOINT,
            data=_urlparse.urlencode(form).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        response = _safe_json(_read_response(self._opener, request))
        token = response.get("access_token")
        token_type = response.get("token_type")
        expires_in = response.get("expires_in")
        granted_scope = response.get("scope")
        expected_scopes = {GOOGLE_SCOPES[scope] for scope in self.scopes}
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in token)
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or type(expires_in) is not int
            or not 1 <= expires_in <= 86_400
            or (granted_scope is not None and (not isinstance(granted_scope, str) or set(granted_scope.split()) != expected_scopes))
        ):
            raise _AuthError("OAuth provider returned invalid credentials or scope.")
        self._access_token = token
        self._token_expires_at = _time.monotonic() + expires_in
        self._revoked = False

    def _bearer_token(self) -> str:
        if not self.connected:
            raise _AuthError("OAuth connector is not connected.")
        assert self._access_token is not None
        return self._access_token

    def _google_api_get(self, url: str) -> dict:
        """GET only allowlisted Google API URLs; bearer tokens never go elsewhere."""
        parsed = _urlparse.urlsplit(url)
        drive_ok = parsed.scheme == "https" and parsed.netloc == "www.googleapis.com" and parsed.path == "/drive/v3/files"
        gmail_path = "/gmail/v1/users/me/messages"
        gmail_ok = False
        if parsed.scheme == "https" and parsed.netloc == "gmail.googleapis.com":
            if parsed.path == gmail_path:
                gmail_ok = True
            elif parsed.path.startswith(gmail_path + "/"):
                message_id = parsed.path[len(gmail_path) + 1:]
                gmail_ok = "/" not in message_id and 1 <= len(message_id) <= 128 and all(c.isalnum() or c in "_-" for c in _urlparse.unquote(message_id))
        if not (drive_ok or gmail_ok):
            raise ConnectorError("OAuth API URL is not allowlisted.")
        request = _urlrequest.Request(url, headers={"Authorization": f"Bearer {self._bearer_token()}", "Accept": "application/json"}, method="GET")
        return _safe_json(_read_response(self._opener, request))

    def revoke(self) -> None:
        token, self._access_token = self._access_token, None
        self._token_expires_at = None
        self._pending = None
        self._revoked = True
        if token is None:
            return
        request = _urlrequest.Request(
            GOOGLE_REVOCATION_ENDPOINT,
            data=_urlparse.urlencode({"token": token}).encode("ascii"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            _read_response(self._opener, request)
        except ConnectorError as exc:
            raise _AuthError("Local OAuth credentials were removed, but remote revocation could not be confirmed.") from exc


def github_api_get(url: str, *, token: bytes, opener: _Callable | None = None) -> dict:
    """GET only ``api.github.com`` URLs with a PAT; read-only helper (P2-2)."""
    parsed = _urlparse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path not in {"/search/issues"}:
        raise ConnectorError("GitHub API URL is not allowlisted.")
    request = _urlrequest.Request(
        url,
        headers={"Authorization": f"Bearer {token.decode('ascii')}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        method="GET",
    )
    return _safe_json(_read_response(opener or _default_opener, request))
