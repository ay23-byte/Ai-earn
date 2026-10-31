"""Experimental local task plans; no execution, queue, or completion.

Trusted application code must supply a fixed auth/state pair. This internal
service is not an OS boundary, web API, or approval for real customer data.
"""
import hmac
import secrets
import sqlite3

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError


_RESOURCE = "task_plan"


class TaskError(Exception):
    """Invalid task input, conflicting retry, or disallowed transition."""


def _input(validator, value):
    # Share the existing bounded input format without exposing draft-specific errors.
    try:
        return validator(value)
    except DraftError as exc:
        raise TaskError("Invalid task input.") from exc


def _check_version(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14):
        raise StateStorageError("State schema version 4, 5, 6, 7, 8, 9, 10, 11, 12, 13 or 14 is required for task plans.")


def _owned(conn: sqlite3.Connection, owner: str, record_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, title, status, created_at, updated_at FROM task_plans WHERE id=? AND owner_id=?",
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
        raise TaskError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM task_plans WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
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


class TaskPlans:
    """Owner-scoped plans only; cancellation never executes or completes work."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, title: str, *, operation_key: str) -> str:
        title = _input(_title, title)
        key = _input(_key, operation_key)
        digest = _digest("task_create", title)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _check_version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            record_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO task_plans(id, owner_id, title, status, created_at, updated_at) VALUES(?,?,?,'planned',?,?)",
                (record_id, owner, title, now, now),
            )
            _audit(conn, now, owner, "task_planned", record_id)
            _remember(conn, owner, key, digest, record_id)
            return record_id

    def get(self, token: str, record_id: str) -> dict[str, str | int]:
        record_id = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _check_version(conn)
            return dict(_owned(conn, owner, record_id))

    def cancel(self, token: str, record_id: str, *, operation_key: str) -> str:
        record_id = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _digest("task_cancel", record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _check_version(conn)
            row = _owned(conn, owner, record_id)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if row["status"] != "planned":
                raise TaskError("Transition is not allowed.")
            conn.execute("UPDATE task_plans SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?", (now, record_id, owner))
            _audit(conn, now, owner, "task_cancelled", record_id)
            _remember(conn, owner, key, digest, record_id)
            return record_id
