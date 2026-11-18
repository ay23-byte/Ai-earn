"""Experimental product workspace (v17) — owner-scoped, session-authorized."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
from typing import Any

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE = "product"
_STATUSES = frozenset({"draft", "published", "archived"})
_TRANSITIONS: dict[tuple[Any, str], str] = {
    (None, "product_created"): "draft",
    ("draft", "product_published"): "published",
    ("draft", "product_archived"): "archived",
    ("published", "product_archived"): "archived",
}


class ProductError(Exception):
    pass


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, ProductError) as exc:
        raise ProductError(str(exc)) from exc
    except Exception as exc:
        raise ProductError("Invalid product input.") from exc


def _description(value: str) -> str:
    if not isinstance(value, str):
        raise ProductError("Invalid description.")
    if len(value) > 2000:
        raise ProductError("Invalid description.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProductError("Invalid description.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProductError("Invalid description.") from exc
    return value


def _version_str(value: str) -> str:
    if not isinstance(value, str):
        raise ProductError("Invalid version.")
    if not 1 <= len(value) <= 20:
        raise ProductError("Invalid version.")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", value):
        raise ProductError("Invalid version.")
    return value


def _price(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
        raise ProductError("Invalid price amount.")
    return value


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 17:
        raise StateStorageError("State schema version 17 is required for products.")


def _retry(conn: sqlite3.Connection, owner: str, key: str, digest: bytes) -> str | None:
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise ProductError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM products WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
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
    row = conn.execute("SELECT * FROM products WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE, record_id),
    ).fetchall()
    if not events:
        raise StateStorageError("Product history is inconsistent.")
    replayed = None
    for (event,) in events:
        step = (replayed, event)
        if step not in _TRANSITIONS:
            raise StateStorageError("Product history is inconsistent.")
        replayed = _TRANSITIONS[step]
    if replayed != row["status"]:
        raise StateStorageError("Product history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Product history is inconsistent.")
    return row


class Products:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, title: str, description: str, version: str, price_paise: int, *, operation_key: str) -> str:
        title = _input(_title, title)
        desc = _input(_description, description)
        ver = _input(_version_str, version)
        price = _input(_price, price_paise)
        key = _input(_key, operation_key)
        digest = _request("product_create", [title, desc, ver, price])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO products(id, owner_id, title, description, version, price_paise, currency, status, created_at, updated_at) VALUES(?,?,?,?,?,?, 'INR', 'draft', ?, ?)",
                (ident, owner, title, desc, ver, price, now, now),
            )
            _audit(conn, now, owner, "product_created", ident)
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
            raise ProductError("Invalid page size.")
        if status is not None and status not in _STATUSES:
            raise ProductError("Invalid status filter.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM products WHERE owner_id=? AND id>? COLLATE BINARY"
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
            raise ProductError("Invalid status.")
        digest = _request("product_transition", [ident, target])
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
                raise ProductError("Transition is not allowed.")
            conn.execute("UPDATE products SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def publish(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "published", "product_published", operation_key)

    def archive(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("product_transition", [ident, "archived"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            if (current, "product_archived") not in _TRANSITIONS:
                raise ProductError("Transition is not allowed.")
            conn.execute("UPDATE products SET status='archived', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "product_archived", ident)
            _remember(conn, owner, key, digest, ident)
            return ident
