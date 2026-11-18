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


def _due_at(value: int | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 4102444800:
        raise InvoiceError("Invalid due_at.")
    return value


def _billing_name(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or len(value.strip()) == 0:
        raise InvoiceError("Invalid billing_name.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvoiceError("Invalid billing_name.")
    return value


def _billing_contact(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        raise InvoiceError("Invalid billing_contact.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InvoiceError("Invalid billing_contact.")
    return value


def _billing_address(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1000:
        raise InvoiceError("Invalid billing_address.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise InvoiceError("Invalid billing_address.")
    return value


def _tax_rate_bps(value: int | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 10000:
        raise InvoiceError("Invalid tax_rate_bps.")
    return value


def _tax_amount_paise(value: int | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
        raise InvoiceError("Invalid tax_amount_paise.")
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


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_bytes(invoice_number: str, order_id: str, owner: str, amount: int, *, due_at: int | None = None, billing_name: str | None = None, billing_contact: str | None = None, billing_address: str | None = None, tax_rate_bps: int | None = None, tax_amount_paise: int | None = None) -> bytes:
    """Render a real, deterministic single-page PDF invoice (stdlib only)."""
    lines = [f"INVOICE {invoice_number}", f"Order {order_id}", f"Owner {owner}", f"Amount {amount} INR"]
    if due_at is not None:
        lines.append(f"Due {due_at}")
    if billing_name is not None:
        lines.append(f"BillingName {billing_name}")
    if billing_contact is not None:
        lines.append(f"BillingContact {billing_contact}")
    if billing_address is not None:
        lines.append(f"BillingAddress {billing_address}")
    if tax_rate_bps is not None:
        lines.append(f"TaxRate {tax_rate_bps}bps")
    if tax_amount_paise is not None:
        lines.append(f"TaxAmount {tax_amount_paise}")
    text_ops = ["BT", "/F1 14 Tf", "72 770 Td"]
    for index, line in enumerate(lines):
        if index:
            text_ops.append("0 -22 Td")
        safe = _pdf_escape("".join(c if 32 <= ord(c) < 127 else "?" for c in line))
        text_ops.append(f"({safe}) Tj")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


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
    # Build replay: start None, then apply each event
    current = None
    for ev in events:
        event_name = ev[0]
        # Non-status financial updates (billing/due/tax) do not affect status, allow them
        if event_name in ("invoice_billing_updated", "invoice_due_updated", "invoice_tax_updated"):
            continue
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

    def create(self, token: str, order_id: str, *, operation_key: str, due_at: int | None = None, billing_name: str | None = None, billing_contact: str | None = None, billing_address: str | None = None, tax_rate_bps: int | None = None, tax_amount_paise: int | None = None) -> str:
        oid = _input(_order_id, order_id)
        key = _input(_key, operation_key)
        due = _input(_due_at, due_at) if due_at is not None else None
        bn = _input(_billing_name, billing_name) if billing_name is not None else None
        bc = _input(_billing_contact, billing_contact) if billing_contact is not None else None
        ba = _input(_billing_address, billing_address) if billing_address is not None else None
        tr = _input(_tax_rate_bps, tax_rate_bps) if tax_rate_bps is not None else None
        ta = _input(_tax_amount_paise, tax_amount_paise) if tax_amount_paise is not None else None
        # Idempotency digest includes all financial fields to prevent reuse with different billing
        digest = _request("invoice_create", [oid, str(due), bn or "", bc or "", ba or "", str(tr), str(ta)])
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
            # Validate due_at not in past? Allow future; if not supplied, None
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= 28:
                conn.execute(
                    "INSERT INTO invoices(id, owner_id, order_id, invoice_number, amount_paise, currency, status, pdf_hash, created_at, updated_at, due_at, billing_name, billing_contact, billing_address, tax_rate_bps, tax_amount_paise) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ident, owner, oid, invoice_number, order["proposed_amount_paise"], order["currency"], "draft", None, now, now, due, bn, bc, ba, tr, ta),
                )
            else:
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
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= 28:
                pdf = _pdf_bytes(row["invoice_number"], row["order_id"], owner, row["amount_paise"], due_at=row["due_at"], billing_name=row["billing_name"], billing_contact=row["billing_contact"], billing_address=row["billing_address"], tax_rate_bps=row["tax_rate_bps"], tax_amount_paise=row["tax_amount_paise"])
            else:
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
        """Return deterministic real PDF bytes for an issued/paid/overdue invoice."""
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            if row["status"] not in ("issued", "paid", "overdue"):
                raise InvoiceError("PDF is available only for issued, paid or overdue invoices.")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= 28:
                stub = _pdf_bytes(row["invoice_number"], row["order_id"], owner, row["amount_paise"], due_at=row["due_at"], billing_name=row["billing_name"], billing_contact=row["billing_contact"], billing_address=row["billing_address"], tax_rate_bps=row["tax_rate_bps"], tax_amount_paise=row["tax_amount_paise"])
            else:
                stub = _pdf_bytes(row["invoice_number"], row["order_id"], owner, row["amount_paise"])
            if row["pdf_hash"] is not None and not hmac.compare_digest(hashlib.sha256(stub).digest(), row["pdf_hash"]):
                raise StateStorageError("Invoice PDF hash is inconsistent.")
            return stub

    def set_billing(self, token: str, record_id: str, *, billing_name: str | None = None, billing_contact: str | None = None, billing_address: str | None = None, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        bn = _input(_billing_name, billing_name) if billing_name is not None else None
        bc = _input(_billing_contact, billing_contact) if billing_contact is not None else None
        ba = _input(_billing_address, billing_address) if billing_address is not None else None
        if bn is None and bc is None and ba is None:
            raise InvoiceError("No billing fields provided.")
        digest = _request("invoice_billing", [ident, bn or "", bc or "", ba or ""])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            if conn.execute("PRAGMA user_version").fetchone()[0] < 28:
                raise StateStorageError("State schema version 28 is required for finance.")
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return ident
            if row["status"] != "draft":
                raise InvoiceError("Billing can only be set on draft invoices.")
            updates = {}
            if bn is not None:
                updates["billing_name"] = bn
            if bc is not None:
                updates["billing_contact"] = bc
            if ba is not None:
                updates["billing_address"] = ba
            set_clause = ", ".join(f"{k}=?" for k in updates) + ", updated_at=?"
            params = list(updates.values()) + [now, ident, owner]
            conn.execute(f"UPDATE invoices SET {set_clause} WHERE id=? AND owner_id=?", tuple(params))
            _audit(conn, now, owner, "invoice_billing_updated", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def set_due_date(self, token: str, record_id: str, due_at: int, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        due = _input(_due_at, due_at)
        if due is None:
            raise InvoiceError("Invalid due_at.")
        digest = _request("invoice_due", [ident, due])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            if conn.execute("PRAGMA user_version").fetchone()[0] < 28:
                raise StateStorageError("State schema version 28 is required for finance.")
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return ident
            if row["status"] != "draft":
                raise InvoiceError("Due date can only be set on draft invoices.")
            conn.execute("UPDATE invoices SET due_at=?, updated_at=? WHERE id=? AND owner_id=?", (due, now, ident, owner))
            _audit(conn, now, owner, "invoice_due_updated", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def set_tax(self, token: str, record_id: str, *, tax_rate_bps: int | None = None, tax_amount_paise: int | None = None, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        tr = _input(_tax_rate_bps, tax_rate_bps) if tax_rate_bps is not None else None
        ta = _input(_tax_amount_paise, tax_amount_paise) if tax_amount_paise is not None else None
        if tr is None and ta is None:
            raise InvoiceError("No tax fields provided.")
        digest = _request("invoice_tax", [ident, str(tr), str(ta)])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            if conn.execute("PRAGMA user_version").fetchone()[0] < 28:
                raise StateStorageError("State schema version 28 is required for finance.")
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return ident
            if row["status"] != "draft":
                raise InvoiceError("Tax can only be set on draft invoices.")
            updates = {}
            if tr is not None:
                updates["tax_rate_bps"] = tr
            if ta is not None:
                updates["tax_amount_paise"] = ta
            set_clause = ", ".join(f"{k}=?" for k in updates) + ", updated_at=?"
            params = list(updates.values()) + [now, ident, owner]
            conn.execute(f"UPDATE invoices SET {set_clause} WHERE id=? AND owner_id=?", tuple(params))
            _audit(conn, now, owner, "invoice_tax_updated", ident)
            _remember(conn, owner, key, digest, ident)
            return ident
