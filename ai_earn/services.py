"""Experimental service catalog & portfolio (v16) — owner-scoped, session-authorized.

Services are **local drafts**, not verified offerings. Portfolio items are
demonstration artifacts linked to a service. All amounts are INR proposals,
not revenue.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from typing import Any

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE_SERVICE = "service"
_RESOURCE_PORTFOLIO = "portfolio_item"
_STATUSES = frozenset({"draft", "published", "archived"})
_TRANSITIONS: dict[tuple[Any, str], str] = {
    (None, "service_created"): "draft",
    ("draft", "service_published"): "published",
    ("draft", "service_archived"): "archived",
    ("published", "service_archived"): "archived",
}


class ServiceError(Exception):
    """Invalid input, conflicting key, or disallowed transition."""


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, ServiceError) as exc:
        raise ServiceError(str(exc)) from exc
    except Exception as exc:
        raise ServiceError("Invalid service input.") from exc


def _scope(value: str) -> str:
    if not isinstance(value, str):
        raise ServiceError("Invalid scope.")
    if len(value) > 2000:
        raise ServiceError("Invalid scope.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServiceError("Invalid scope.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ServiceError("Invalid scope.") from exc
    return value


def _deliverable(value: str) -> str:
    if not isinstance(value, str):
        raise ServiceError("Invalid deliverable.")
    value_stripped = value.strip()
    if not 1 <= len(value) <= 500 or len(value_stripped) == 0:
        raise ServiceError("Invalid deliverable.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServiceError("Invalid deliverable.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ServiceError("Invalid deliverable.") from exc
    return value


def _price(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
        raise ServiceError("Invalid price amount.")
    return value


def _revisions(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 100:
        raise ServiceError("Invalid revisions.")
    return value


def _description(value: str) -> str:
    if not isinstance(value, str):
        raise ServiceError("Invalid description.")
    if len(value) > 2000:
        raise ServiceError("Invalid description.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServiceError("Invalid description.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ServiceError("Invalid description.") from exc
    return value


def _version(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 16:
        raise StateStorageError("State schema version 16 is required for services.")


def _retry(conn: sqlite3.Connection, owner: str, key: str, digest: bytes, resource: str) -> str | None:
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != resource or not hmac.compare_digest(row["request_hash"], digest):
        raise ServiceError("Operation key was already used for a different request.")
    # Verify result still exists and owned
    table = "services" if resource == _RESOURCE_SERVICE else "portfolio_items"
    if conn.execute(f"SELECT 1 FROM {table} WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]


def _audit(conn: sqlite3.Connection, now: int, owner: str, event: str, resource: str, record_id: str) -> None:
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, event, resource, record_id),
    )


def _remember(conn: sqlite3.Connection, owner: str, key: str, digest: bytes, resource: str, record_id: str) -> None:
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, resource, record_id),
    )


def _request(action: str, values: list) -> bytes:
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _owned_service(conn: sqlite3.Connection, owner: str, record_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM services WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE_SERVICE, record_id),
    ).fetchall()
    if not events:
        raise StateStorageError("Service history is inconsistent.")
    replayed = None
    for (event,) in events:
        step = (replayed, event)
        if step not in _TRANSITIONS:
            raise StateStorageError("Service history is inconsistent.")
        replayed = _TRANSITIONS[step]
    if replayed != row["status"]:
        raise StateStorageError("Service history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE_SERVICE, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Service history is inconsistent.")
    return row


class Services:
    """Owner-scoped service catalog."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(
        self,
        token: str,
        title: str,
        scope: str,
        deliverable: str,
        price_paise: int,
        revisions: int,
        *,
        operation_key: str,
    ) -> str:
        title = _input(_title, title)
        scope_v = _input(_scope, scope)
        deliverable_v = _input(_deliverable, deliverable)
        price = _input(_price, price_paise)
        rev = _input(_revisions, revisions)
        key = _input(_key, operation_key)
        digest = _request("service_create", [title, scope_v, deliverable_v, price, rev])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_SERVICE)
            if existing is not None:
                return existing
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO services(id, owner_id, title, scope, deliverable, price_paise, currency, revisions, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?, 'INR', ?, 'draft', ?, ?)",
                (ident, owner, title, scope_v, deliverable_v, price, rev, now, now),
            )
            _audit(conn, now, owner, "service_created", _RESOURCE_SERVICE, ident)
            _remember(conn, owner, key, digest, _RESOURCE_SERVICE, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            return dict(_owned_service(conn, owner, ident))

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20, status: str | None = None) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ServiceError("Invalid page size.")
        if status is not None and status not in _STATUSES:
            raise ServiceError("Invalid status filter.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            query = "SELECT id FROM services WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if status is not None:
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(_owned_service(conn, owner, r["id"])) for r in rows]

    def _transition(self, token: str, record_id: str, target: str, event: str, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        if target not in _STATUSES:
            raise ServiceError("Invalid status.")
        digest = _request("service_transition", [ident, target])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned_service(conn, owner, ident)
            existing = _retry(conn, owner, key, digest, _RESOURCE_SERVICE)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            expected = _TRANSITIONS.get((current, event))
            if expected != target:
                raise ServiceError("Transition is not allowed.")
            conn.execute("UPDATE services SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, ident, owner))
            _audit(conn, now, owner, event, _RESOURCE_SERVICE, ident)
            _remember(conn, owner, key, digest, _RESOURCE_SERVICE, ident)
            return ident

    def publish(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "published", "service_published", operation_key)

    def archive(self, token: str, record_id: str, *, operation_key: str) -> str:
        # allow archiving from draft or published
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("service_transition", [ident, "archived"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned_service(conn, owner, ident)
            existing = _retry(conn, owner, key, digest, _RESOURCE_SERVICE)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            if (current, "service_archived") not in _TRANSITIONS:
                raise ServiceError("Transition is not allowed.")
            conn.execute("UPDATE services SET status='archived', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "service_archived", _RESOURCE_SERVICE, ident)
            _remember(conn, owner, key, digest, _RESOURCE_SERVICE, ident)
            return ident


class Portfolio:
    """Portfolio items linked to services."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(
        self,
        token: str,
        service_id: str,
        title: str,
        description: str,
        *,
        operation_key: str,
    ) -> str:
        sid = _input(_record_id, service_id)
        title_v = _input(_title, title)
        desc_v = _input(_description, description)
        key = _input(_key, operation_key)
        digest = _request("portfolio_create", [sid, title_v, desc_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_PORTFOLIO)
            if existing is not None:
                return existing
            # Verify service owned and exists
            _owned_service(conn, owner, sid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO portfolio_items(id, owner_id, service_id, title, description, created_at) VALUES(?,?,?,?,?,?)",
                (ident, owner, sid, title_v, desc_v, now),
            )
            _audit(conn, now, owner, "portfolio_item_created", _RESOURCE_PORTFOLIO, ident)
            _remember(conn, owner, key, digest, _RESOURCE_PORTFOLIO, ident)
            return ident

    def list(
        self,
        token: str,
        *,
        service_id: str | None = None,
        after_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if service_id is not None:
            _input(_record_id, service_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ServiceError("Invalid page size.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            query = "SELECT id FROM portfolio_items WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if service_id is not None:
                query += " AND service_id=?"
                params.append(service_id)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            out = []
            for r in rows:
                row = conn.execute("SELECT * FROM portfolio_items WHERE id=? AND owner_id=?", (r["id"], owner)).fetchone()
                if row is None:
                    raise AccessDenied("Access denied.")
                out.append(dict(row))
            return out
