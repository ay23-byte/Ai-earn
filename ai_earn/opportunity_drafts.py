"""Experimental local opportunity drafts; no discovery, verification or outreach.

Trusted application code must supply a fixed auth/state pair. This internal
service is not an OS boundary, a web API, or approval for real customer data.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3

from .auth import AccessDenied, AuthService
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError


_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_RESOURCE = "opportunity_draft"


class DraftError(Exception):
    """Invalid draft input, conflicting retry, or disallowed transition."""


def _key(value: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise DraftError("Operation key must be 1–128 ASCII letters, numbers, underscores or hyphens.")
    return value


def _record_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise DraftError("Invalid draft identifier.")
    return value


def _title(value: str) -> str:
    if not isinstance(value, str):
        raise DraftError("Invalid draft title.")
    value = value.strip()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DraftError("Invalid draft title.") from exc
    if not 1 <= len(value) <= 200 or len(encoded) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise DraftError("Invalid draft title.")
    return value


def _digest(action: str, value: str) -> bytes:
    payload = json.dumps([action, value], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).digest()


def _check_version(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (3, 4, 5, 6, 7, 8):
        raise StateStorageError("State schema version 3, 4, 5, 6, 7 or 8 is required for drafts.")


def _owned(conn: sqlite3.Connection, owner: str, record_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, title, status, created_at, updated_at FROM opportunity_drafts WHERE id=? AND owner_id=?",
        (record_id, owner),
    ).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    return row


def _retry(conn: sqlite3.Connection, owner: str, key: str, digest: bytes) -> str | None:
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise DraftError("Operation key was already used for a different request.")
    # A malformed or incomplete database must not be treated as a successful retry.
    if conn.execute("SELECT 1 FROM opportunity_drafts WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]


def _audit(conn: sqlite3.Connection, now: int, owner: str, event: str, record_id: str) -> None:
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, event, _RESOURCE, record_id),
    )


def _remember(conn: sqlite3.Connection, owner: str, key: str, digest: bytes, record_id: str) -> None:
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, _RESOURCE, record_id),
    )


class OpportunityDrafts:
    """Store only user-entered, unverified drafts with owner-scoped operations."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, title: str, *, operation_key: str) -> str:
        title = _title(title)
        key = _key(operation_key)
        digest = _digest("create", title)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _check_version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            record_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO opportunity_drafts(id, owner_id, title, status, created_at, updated_at) VALUES(?,?,?,'draft',?,?)",
                (record_id, owner, title, now, now),
            )
            _audit(conn, now, owner, "draft_created", record_id)
            _remember(conn, owner, key, digest, record_id)
            return record_id

    def get(self, token: str, record_id: str) -> dict[str, str | int]:
        record_id = _record_id(record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _check_version(conn)
            return dict(_owned(conn, owner, record_id))

    def archive(self, token: str, record_id: str, *, operation_key: str) -> str:
        record_id = _record_id(record_id)
        key = _key(operation_key)
        digest = _digest("archive", record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _check_version(conn)
            row = _owned(conn, owner, record_id)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if row["status"] != "draft":
                raise DraftError("Transition is not allowed.")
            conn.execute("UPDATE opportunity_drafts SET status='archived', updated_at=? WHERE id=? AND owner_id=?", (now, record_id, owner))
            _audit(conn, now, owner, "draft_archived", record_id)
            _remember(conn, owner, key, digest, record_id)
            return record_id
