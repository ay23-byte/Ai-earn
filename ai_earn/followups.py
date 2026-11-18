"""Experimental CRM followups (v19) — owner-scoped, session-authorized."""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from typing import Any

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE = "followup"
_STATUSES = frozenset({"pending", "completed", "cancelled"})
_TRANSITIONS: dict[tuple[Any, str], str] = {
    (None, "followup_created"): "pending",
    ("pending", "followup_completed"): "completed",
    ("pending", "followup_cancelled"): "cancelled",
}


class FollowupError(Exception):
    pass


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, FollowupError) as exc:
        raise FollowupError(str(exc)) from exc
    except Exception as exc:
        raise FollowupError("Invalid followup input.") from exc


def _due_at(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 4102444800:
        raise FollowupError("Invalid due_at.")
    return value


def _note(value: str) -> str:
    if not isinstance(value, str):
        raise FollowupError("Invalid note.")
    if len(value) > 500:
        raise FollowupError("Invalid note.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise FollowupError("Invalid note.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FollowupError("Invalid note.") from exc
    return value


def _customer_id(value: str) -> str:
    return _record_id(value)


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 19:
        raise StateStorageError("State schema version 19 is required for followups.")


def _retry(conn, owner, key, digest):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise FollowupError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM followups WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]


def _audit(conn, now, owner, event, record_id):
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, event, _RESOURCE, record_id),
    )


def _remember(conn, owner, key, digest, record_id):
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, _RESOURCE, record_id),
    )


def _request(action: str, values: list) -> bytes:
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _owned(conn, owner, record_id):
    row = conn.execute("SELECT * FROM followups WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE, record_id),
    ).fetchall()
    if not events:
        raise StateStorageError("Followup history is inconsistent.")
    replayed = None
    for (event,) in events:
        step = (replayed, event)
        if step not in _TRANSITIONS:
            raise StateStorageError("Followup history is inconsistent.")
        replayed = _TRANSITIONS[step]
    if replayed != row["status"]:
        raise StateStorageError("Followup history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Followup history is inconsistent.")
    return row


class Followups:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, customer_id: str, due_at: int, note: str = "", *, operation_key: str) -> str:
        cid = _input(_customer_id, customer_id)
        due = _input(_due_at, due_at)
        note_v = _input(_note, note)
        key = _input(_key, operation_key)
        digest = _request("followup_create", [cid, due, note_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            # Validate customer belongs to owner
            if conn.execute("SELECT 1 FROM customers WHERE id=? AND owner_id=?", (cid, owner)).fetchone() is None:
                raise AccessDenied("Access denied.")
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO followups(id, owner_id, customer_id, due_at, note, status, created_at, updated_at) VALUES(?,?,?,?,?, 'pending', ?, ?)",
                (ident, owner, cid, due, note_v, now, now),
            )
            _audit(conn, now, owner, "followup_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(_owned(conn, owner, ident))

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20, customer_id: str | None = None, status: str | None = None) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FollowupError("Invalid page size.")
        if customer_id is not None:
            customer_id = _input(_customer_id, customer_id)
        if status is not None and status not in _STATUSES:
            raise FollowupError("Invalid status filter.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM followups WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if customer_id is not None:
                query += " AND customer_id=?"
                params.append(customer_id)
            if status is not None:
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(_owned(conn, owner, r["id"])) for r in rows]

    def _transition(self, token, record_id, target, event, operation_key):
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        if target not in _STATUSES:
            raise FollowupError("Invalid status.")
        digest = _request("followup_transition", [ident, target])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            expected = _TRANSITIONS.get((current, event))
            if expected != target:
                raise FollowupError("Transition is not allowed.")
            conn.execute("UPDATE followups SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def completed(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "completed", "followup_completed", operation_key)

    def cancelled(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "cancelled", "followup_cancelled", operation_key)
