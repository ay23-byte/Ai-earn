"""Experimental ledger (v18) — owner-scoped, session-authorized, net calculation."""

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

_RESOURCE = "ledger_entry"
_KINDS = frozenset({"income", "expense", "refund", "fee"})


class LedgerError(Exception):
    pass


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, LedgerError) as exc:
        raise LedgerError(str(exc)) from exc
    except Exception as exc:
        raise LedgerError("Invalid ledger input.") from exc


def _kind(value: str) -> str:
    if value not in _KINDS:
        raise LedgerError("Invalid kind.")
    return value


def _amount(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
        raise LedgerError("Invalid amount.")
    return value


def _note(value: str) -> str:
    if not isinstance(value, str):
        raise LedgerError("Invalid note.")
    if len(value) > 500:
        raise LedgerError("Invalid note.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise LedgerError("Invalid note.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LedgerError("Invalid note.") from exc
    return value


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 18:
        raise StateStorageError("State schema version 18 is required for ledger.")


def _retry(conn, owner, key, digest):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise LedgerError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM ledger_entries WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
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
    row = conn.execute("SELECT * FROM ledger_entries WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    # single creation event, no history replay needed beyond creation check
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE, record_id),
    ).fetchall()
    if not events or events[0][0] != "ledger_entry_created" or len(events) != 1:
        raise StateStorageError("Ledger entry history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE, record_id),
    ).fetchone()[0]
    if count != 1:
        raise StateStorageError("Ledger entry history is inconsistent.")
    return row


class Ledger:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def add(self, token: str, kind: str, amount_paise: int, note: str = "", *, operation_key: str) -> str:
        k = _input(_kind, kind)
        amt = _input(_amount, amount_paise)
        n = _input(_note, note)
        key = _input(_key, operation_key)
        digest = _request("ledger_add", [k, amt, n])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO ledger_entries(id, owner_id, kind, amount_paise, currency, note, created_at) VALUES(?,?,?,?, 'INR', ?, ?)",
                (ident, owner, k, amt, n, now),
            )
            _audit(conn, now, owner, "ledger_entry_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(_owned(conn, owner, ident))

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20, kind: str | None = None) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise LedgerError("Invalid page size.")
        if kind is not None and kind not in _KINDS:
            raise LedgerError("Invalid kind filter.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM ledger_entries WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if kind is not None:
                query += " AND kind=?"
                params.append(kind)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(_owned(conn, owner, r["id"])) for r in rows]

    def net(self, token: str) -> dict:
        """Return net = income - expense - refund - fee, all owner-scoped."""
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            rows = conn.execute(
                "SELECT kind, COALESCE(SUM(amount_paise),0) FROM ledger_entries WHERE owner_id=? GROUP BY kind",
                (owner,),
            ).fetchall()
            totals = {k: 0 for k in _KINDS}
            for kind, total in rows:
                totals[kind] = total
            net_paise = totals["income"] - totals["expense"] - totals["refund"] - totals["fee"]
            count = conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE owner_id=?", (owner,)).fetchone()[0]
            return {"totals": totals, "net_paise": net_paise, "count": count, "currency": "INR"}
