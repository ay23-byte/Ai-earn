"""Experimental invoices (v20) — owner-scoped, session-authorized, auditable, PDF stub."""

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

_RESOURCE = "invoice"
_STATUSES = frozenset({"draft", "issued", "paid", "overdue", "void"})
_TRANSITIONS = {
    (None, "invoice_created"): "draft",
    ("draft", "invoice_issued"): "issued",
    ("draft", "invoice_voided"): "void",
    ("issued", "invoice_paid"): "paid",
    ("issued", "invoice_overdue"): "overdue",
    ("issued", "invoice_voided"): "void",
    ("overdue", "invoice_paid"): "paid",
    ("overdue", "invoice_voided"): "void",
}


class InvoiceError(Exception):
    pass


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, InvoiceError) as exc:
        raise InvoiceError(str(exc)) from exc
    except Exception as exc:
        raise InvoiceError("Invalid invoice input.") from exc


def _order_id(value: str) -> str:
    return _record_id(value)


def _invoice_number(value: str) -> str:
    if not isinstance(value, str):
        raise InvoiceError("Invalid invoice number.")
    if not 1 <= len(value) <= 32:
        raise InvoiceError("Invalid invoice number.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvoiceError("Invalid invoice number.")
    # allow A-Z a-z 0-9 _ -
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise InvoiceError("Invalid invoice number.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvoiceError("Invalid invoice number.") from exc
    return value


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 20:
        raise StateStorageError("State schema version 20 is required for invoices.")


def _retry(conn, owner, key, digest):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise InvoiceError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM invoices WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
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


def _pdf_bytes(invoice_number: str, order_id: str, owner: str, amount: int) -> bytes:
    # Stub PDF content: deterministic text, not a real PDF binary, 11-concept note
    content = f"INVOICE {invoice_number}|Order {order_id}|Owner {owner}|Amount {amount} INR|STUB|11-CONCEPT\n"
    return content.encode("utf-8")


def _owned(conn, owner, record_id):
    row = conn.execute("SELECT * FROM invoices WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    # Verify amount snapshot still matches order draft (if order still exists)
    order = conn.execute("SELECT proposed_amount_paise, currency FROM order_drafts WHERE id=? AND owner_id=?", (row["order_id"], owner)).fetchone()
    if order is None or row["amount_paise"] != order["proposed_amount_paise"] or row["currency"] != order["currency"]:
        # If order was deleted or amount mismatched, treat as integrity error
        # But order deletion should not happen via API; still check
        raise StateStorageError("Invoice amount snapshot is inconsistent or unavailable.")
    # Check history replay
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE, record_id),
    ).fetchall()
    if not events or events[0][0] != "invoice_created":
        raise StateStorageError("Invoice history is inconsistent.")
    # Replay multi-step
    replayed = None
    expected = None
    # Build replay: start None, then apply each event
    current = None
    for ev in events:
        event_name = ev[0]
        step = (current, event_name)
        if step not in _TRANSITIONS:
            raise StateStorageError("Invoice history is inconsistent.")
        current = _TRANSITIONS[step]
    if current != row["status"]:
        raise StateStorageError("Invoice history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Invoice history is inconsistent.")
    # PDF hash consistency: if status in issued/paid/overdue, pdf_hash must be present and match stub?
    # For hardening, we check that pdf_hash is not null for those statuses, and null for draft/void?
    # But we allow void to retain hash if previously issued? For simplicity, check that issued/paid/overdue have hash
    status = row["status"]
    pdf_hash = row["pdf_hash"]
    if status in ("issued", "paid", "overdue"):
        if pdf_hash is None:
            raise StateStorageError("Invoice PDF is missing for issued status.")
        if not isinstance(pdf_hash, bytes) or len(pdf_hash) != 32:
            raise StateStorageError("Invoice PDF hash is inconsistent.")
    # For draft/void, hash may be null or existing (if void after issued, hash remains)
    return row


class Invoices:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, order_id: str, *, operation_key: str) -> str:
        oid = _input(_order_id, order_id)
        key = _input(_key, operation_key)
        digest = _request("invoice_create", [oid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            order = conn.execute(
                "SELECT proposed_amount_paise, currency, status FROM order_drafts WHERE id=? AND owner_id=?",
                (oid, owner),
            ).fetchone()
            if order is None:
                raise AccessDenied("Access denied.")
            if order["status"] != "draft":
                raise InvoiceError("An active order draft is required.")
            ident = secrets.token_hex(16)
            invoice_number = f"INV-{ident[:8].upper()}"
            # Validate generated number
            _invoice_number(invoice_number)
            # Ensure uniqueness per owner: try insert, if conflict due to improbable collision, retry once with new ident?
            # For simplicity, attempt once; if fails due to UNIQUE, raise error (client can retry with new key? but operation_key same would not retry)
            # Instead we generate and check existence before insert
            if conn.execute("SELECT 1 FROM invoices WHERE invoice_number=? AND owner_id=?", (invoice_number, owner)).fetchone() is not None:
                # Extremely unlikely, try second generation
                ident = secrets.token_hex(16)
                invoice_number = f"INV-{ident[:8].upper()}"
            conn.execute(
                "INSERT INTO invoices(id, owner_id, order_id, invoice_number, amount_paise, currency, status, pdf_hash, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, owner, oid, invoice_number, order["proposed_amount_paise"], order["currency"], "draft", None, now, now),
            )
            _audit(conn, now, owner, "invoice_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(_owned(conn, owner, ident))

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20, status: str | None = None, order_id: str | None = None) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvoiceError("Invalid page size.")
        if status is not None and status not in _STATUSES:
            raise InvoiceError("Invalid status filter.")
        if order_id is not None:
            order_id = _input(_order_id, order_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM invoices WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if status is not None:
                query += " AND status=?"
                params.append(status)
            if order_id is not None:
                query += " AND order_id=?"
                params.append(order_id)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(_owned(conn, owner, r["id"])) for r in rows]

    def issued(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("invoice_issued", [ident])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            if (row["status"], "invoice_issued") not in _TRANSITIONS:
                raise InvoiceError("Transition is not allowed.")
            pdf = _pdf_bytes(row["invoice_number"], row["order_id"], owner, row["amount_paise"])
            pdf_hash = hashlib.sha256(pdf).digest()
            conn.execute("UPDATE invoices SET status='issued', pdf_hash=?, updated_at=? WHERE id=? AND owner_id=?", (pdf_hash, now, ident, owner))
            _audit(conn, now, owner, "invoice_issued", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def paid(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("invoice_paid", [ident])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            if (row["status"], "invoice_paid") not in _TRANSITIONS:
                raise InvoiceError("Transition is not allowed.")
            # Keep pdf_hash as is (issued hash), but ensure it exists
            if row["pdf_hash"] is None:
                raise StateStorageError("Invoice PDF is missing.")
            conn.execute("UPDATE invoices SET status='paid', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "invoice_paid", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def overdue(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("invoice_overdue", [ident])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            if (row["status"], "invoice_overdue") not in _TRANSITIONS:
                raise InvoiceError("Transition is not allowed.")
            if row["pdf_hash"] is None:
                raise StateStorageError("Invoice PDF is missing.")
            conn.execute("UPDATE invoices SET status='overdue', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "invoice_overdue", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def voided(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("invoice_voided", [ident])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            if (row["status"], "invoice_voided") not in _TRANSITIONS:
                raise InvoiceError("Transition is not allowed.")
            conn.execute("UPDATE invoices SET status='void', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "invoice_voided", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def pdf_stub(self, token: str, record_id: str) -> bytes:
        """Return deterministic PDF stub bytes for an issued/paid/overdue invoice."""
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            if row["status"] not in ("issued", "paid", "overdue"):
                raise InvoiceError("PDF is available only for issued, paid or overdue invoices.")
            stub = _pdf_bytes(row["invoice_number"], row["order_id"], owner, row["amount_paise"])
            if row["pdf_hash"] is not None and not hmac.compare_digest(hashlib.sha256(stub).digest(), row["pdf_hash"]):
                raise StateStorageError("Invoice PDF hash is inconsistent.")
            return stub
