"""Experimental CRM customers/leads (v17) — owner-scoped, session-authorized."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from typing import Any

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE = "customer"
_STATUSES = frozenset({"lead", "contacted", "converted", "rejected"})
_TRANSITIONS: dict[tuple[Any, str], str] = {
    (None, "customer_created"): "lead",
    ("lead", "customer_contacted"): "contacted",
    ("lead", "customer_rejected"): "rejected",
    ("contacted", "customer_converted"): "converted",
    ("contacted", "customer_rejected"): "rejected",
}


class CustomerError(Exception):
    pass


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, CustomerError) as exc:
        raise CustomerError(str(exc)) from exc
    except Exception as exc:
        raise CustomerError("Invalid customer input.") from exc


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise CustomerError("Invalid name.")
    stripped = value.strip()
    if not 1 <= len(value) <= 200 or len(stripped) == 0:
        raise CustomerError("Invalid name.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CustomerError("Invalid name.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CustomerError("Invalid name.") from exc
    return value


def _contact(value: str) -> str:
    if not isinstance(value, str):
        raise CustomerError("Invalid contact.")
    if not 1 <= len(value) <= 500:
        raise CustomerError("Invalid contact.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CustomerError("Invalid contact.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CustomerError("Invalid contact.") from exc
    return value


def _source(value: str) -> str:
    if not isinstance(value, str):
        raise CustomerError("Invalid source.")
    if len(value) > 500:
        raise CustomerError("Invalid source.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CustomerError("Invalid source.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CustomerError("Invalid source.") from exc
    return value


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 17:
        raise StateStorageError("State schema version 17 is required for customers.")


def _retry(conn, owner, key, digest):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise CustomerError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM customers WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
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
    row = conn.execute("SELECT * FROM customers WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE, record_id),
    ).fetchall()
    if not events:
        raise StateStorageError("Customer history is inconsistent.")
    replayed = None
    for (event,) in events:
        step = (replayed, event)
        if step not in _TRANSITIONS:
            raise StateStorageError("Customer history is inconsistent.")
        replayed = _TRANSITIONS[step]
    if replayed != row["status"]:
        raise StateStorageError("Customer history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Customer history is inconsistent.")
    return row


class Customers:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, name: str, contact: str, source: str = "", *, operation_key: str) -> str:
        name_v = _input(_name, name)
        contact_v = _input(_contact, contact)
        source_v = _input(_source, source)
        key = _input(_key, operation_key)
        digest = _request("customer_create", [name_v, contact_v, source_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO customers(id, owner_id, name, contact, source, status, created_at, updated_at) VALUES(?,?,?,?,?, 'lead', ?, ?)",
                (ident, owner, name_v, contact_v, source_v, now, now),
            )
            _audit(conn, now, owner, "customer_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(_owned(conn, owner, ident))

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20, status: str | None = None) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CustomerError("Invalid page size.")
        if status is not None and status not in _STATUSES:
            raise CustomerError("Invalid status filter.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM customers WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
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
            raise CustomerError("Invalid status.")
        digest = _request("customer_transition", [ident, target])
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
                raise CustomerError("Transition is not allowed.")
            conn.execute("UPDATE customers SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def contacted(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "contacted", "customer_contacted", operation_key)

    def converted(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "converted", "customer_converted", operation_key)

    def rejected(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("customer_transition", [ident, "rejected"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            if (current, "customer_rejected") not in _TRANSITIONS:
                raise CustomerError("Transition is not allowed.")
            conn.execute("UPDATE customers SET status='rejected', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "customer_rejected", ident)
            _remember(conn, owner, key, digest, ident)
            return ident
