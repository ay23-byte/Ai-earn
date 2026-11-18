"""Persisted AI provider configuration and token metering (P3-1).

Only a SHA-256 hash of the API key is stored — never the raw key. Live
providers remain blocked by the existing readiness gates; persistence does
not enable any external call.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3

from .auth import AuthService
from .opportunity_drafts import DraftError, _digest, _key
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE = "ai_provider_config"
_PROVIDERS = frozenset({"stub", "ollama", "openai", "hosted"})


class AIProviderConfigError(Exception):
    """Invalid provider configuration or metering input."""


def _version(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] != 29:
        raise StateStorageError("State schema version 29 is required for AI provider configuration.")


def _text(value, *, maximum: int, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AIProviderConfigError(f"Invalid {label}.")
    return value


class AIProviderConfigs:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def configure(self, token: str, *, provider: str, model: str, base_url: str = "", api_key: bytes | str | None = None, operation_key: str) -> dict:
        try:
            key = _key(operation_key)
        except DraftError as exc:
            raise AIProviderConfigError("Invalid operation key.") from exc
        if provider not in _PROVIDERS:
            raise AIProviderConfigError("Unsupported AI provider.")
        model = _text(model, maximum=128, label="model")
        if not isinstance(base_url, str) or len(base_url) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in base_url):
            raise AIProviderConfigError("Invalid base URL.")
        if base_url and not base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise AIProviderConfigError("Base URL must use HTTPS or loopback HTTP.")
        key_hash = None
        if api_key is not None:
            raw = api_key.encode("utf-8") if isinstance(api_key, str) else api_key
            if not isinstance(raw, bytes) or not 32 <= len(raw) <= 128:
                raise AIProviderConfigError("Invalid API key.")
            key_hash = hashlib.sha256(raw).digest()
        digest = _digest("ai_provider_configure", f"{provider}|{model}|{base_url}|{key_hash.hex() if key_hash else ''}")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = conn.execute(
                "SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)
            ).fetchone()
            if existing is not None:
                import hmac as _hmac

                if existing["request_hash"] and not _hmac.compare_digest(existing["request_hash"], digest):
                    raise AIProviderConfigError("Operation key was already used for a different request.")
                return self._current(conn, owner)
            conn.execute(
                "INSERT INTO ai_provider_configs(owner_id, provider, model, base_url, api_key_hash, created_at, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET provider=excluded.provider, model=excluded.model, base_url=excluded.base_url, api_key_hash=excluded.api_key_hash, updated_at=excluded.updated_at",
                (owner, provider, model, base_url, key_hash, now, now),
            )
            conn.execute(
                "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (owner, key, digest, _RESOURCE, owner),
            )
            conn.execute(
                "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (now, owner, "ai_provider_configured", _RESOURCE, owner),
            )
            return self._current(conn, owner)

    @staticmethod
    def _current(conn, owner) -> dict:
        row = conn.execute(
            "SELECT provider, model, base_url, api_key_hash, updated_at FROM ai_provider_configs WHERE owner_id=?", (owner,)
        ).fetchone()
        if row is None:
            return {"provider": "stub", "model": "", "configured": False, "has_api_key": False}
        return {
            "provider": row["provider"],
            "model": row["model"],
            "base_url": row["base_url"],
            "configured": True,
            "has_api_key": row["api_key_hash"] is not None,
            "updated_at": row["updated_at"],
        }

    def current(self, token: str) -> dict:
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            return self._current(conn, owner)

    def record_usage(self, token: str, *, prompt_tokens: int, completion_tokens: int, operation_key: str) -> str:
        try:
            key = _key(operation_key)
        except DraftError as exc:
            raise AIProviderConfigError("Invalid operation key.") from exc
        for value in (prompt_tokens, completion_tokens):
            if type(value) is not int or not 0 <= value <= 100_000_000:
                raise AIProviderConfigError("Invalid token usage.")
        digest = _digest("ai_token_usage", f"{prompt_tokens}|{completion_tokens}")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            config = self._current(conn, owner)
            existing = conn.execute(
                "SELECT resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)
            ).fetchone()
            if existing is not None:
                return existing["resource_id"]
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO ai_token_usage(id, owner_id, provider, model, prompt_tokens, completion_tokens, created_at) VALUES(?,?,?,?,?,?,?)",
                (ident, owner, config["provider"], config["model"], prompt_tokens, completion_tokens, now),
            )
            conn.execute(
                "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                (owner, key, digest, "ai_token_usage", ident),
            )
            return ident

    def usage_totals(self, token: str) -> dict:
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) FROM ai_token_usage WHERE owner_id=?",
                (owner,),
            ).fetchone()
            return {"calls": row[0], "prompt_tokens": row[1], "completion_tokens": row[2], "total_tokens": row[1] + row[2]}
