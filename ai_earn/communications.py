"""Experimental CRM communications (v18) — owner-scoped, session-authorized."""

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

_RESOURCE = "communication"
_STATUSES = frozenset({"draft", "sent", "archived"})
_TRANSITIONS: dict[tuple[Any, str], str] = {
    (None, "communication_created"): "draft",
    ("draft", "communication_sent"): "sent",
    ("draft", "communication_archived"): "archived",
    ("sent", "communication_archived"): "archived",
}


class CommunicationError(Exception):
    pass


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, CommunicationError) as exc:
        raise CommunicationError(str(exc)) from exc
    except Exception as exc:
        raise CommunicationError("Invalid communication input.") from exc


def _channel(value: str) -> str:
    if not isinstance(value, str):
        raise CommunicationError("Invalid channel.")
    if not 1 <= len(value) <= 30:
        raise CommunicationError("Invalid channel.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CommunicationError("Invalid channel.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CommunicationError("Invalid channel.") from exc
    if value.strip() == "":
        raise CommunicationError("Invalid channel.")
    return value


def _body(value: str) -> str:
    if not isinstance(value, str):
        raise CommunicationError("Invalid body.")
    if not 1 <= len(value) <= 2000:
        raise CommunicationError("Invalid body.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CommunicationError("Invalid body.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CommunicationError("Invalid body.") from exc
    return value


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 18:
        raise StateStorageError("State schema version 18 is required for communications.")


def _retry(conn, owner, key, digest):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise CommunicationError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM communications WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
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
    row = conn.execute("SELECT * FROM communications WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE, record_id),
    ).fetchall()
    if not events:
        raise StateStorageError("Communication history is inconsistent.")
    replayed = None
    for (event,) in events:
        step = (replayed, event)
        if step not in _TRANSITIONS:
            raise StateStorageError("Communication history is inconsistent.")
        replayed = _TRANSITIONS[step]
    if replayed != row["status"]:
        raise StateStorageError("Communication history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Communication history is inconsistent.")
    return row


class Communications:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, customer_id: str, channel: str, body: str, *, operation_key: str) -> str:
        cust = _input(_record_id, customer_id)
        chan = _input(_channel, channel)
        b = _input(_body, body)
        key = _input(_key, operation_key)
        digest = _request("communication_create", [cust, chan, b])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            # customer must exist and be owned
            if conn.execute("SELECT 1 FROM customers WHERE id=? AND owner_id=?", (cust, owner)).fetchone() is None:
                raise AccessDenied("Access denied.")
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO communications(id, owner_id, customer_id, channel, body, status, created_at, updated_at) VALUES(?,?,?,?,?, 'draft', ?, ?)",
                (ident, owner, cust, chan, b, now, now),
            )
            _audit(conn, now, owner, "communication_created", ident)
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
            raise CommunicationError("Invalid page size.")
        if status is not None and status not in _STATUSES:
            raise CommunicationError("Invalid status filter.")
        if customer_id is not None:
            _input(_record_id, customer_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM communications WHERE owner_id=? AND id>? COLLATE BINARY"
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
            raise CommunicationError("Invalid status.")
        digest = _request("communication_transition", [ident, target])
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
                raise CommunicationError("Transition is not allowed.")
            conn.execute("UPDATE communications SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def sent(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "sent", "communication_sent", operation_key)

    def archive(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("communication_transition", [ident, "archived"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            if (current, "communication_archived") not in _TRANSITIONS:
                raise CommunicationError("Transition is not allowed.")
            conn.execute("UPDATE communications SET status='archived', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "communication_archived", ident)
            _remember(conn, owner, key, digest, ident)
            return ident
