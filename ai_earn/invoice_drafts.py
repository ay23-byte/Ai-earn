"""Internal disposable invoice drafts, not issued invoices or payment requests.

Trusted code must supply a fixed auth/state pair. No customer data, sending,
financial verification, or production use is supported.
"""
import hmac
import secrets
import sqlite3

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError


_RESOURCE = "invoice_draft"


class InvoiceDraftError(Exception):
    """Invalid input, conflicting retry, or disallowed draft transition."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise InvoiceDraftError("Invalid invoice draft input.") from exc


def _check_version(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21):
        raise StateStorageError("State schema version 6, 7, 8, 9, 10, 11, 12, 13, 14 or 15 is required for invoice drafts.")


def _owned(conn: sqlite3.Connection, owner: str, record_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, order_id, proposed_amount_paise, currency, status, created_at, updated_at "
        "FROM invoice_drafts WHERE id=? AND owner_id=?", (record_id, owner),
    ).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    parent = conn.execute(
        "SELECT proposed_amount_paise, currency FROM order_drafts WHERE id=? AND owner_id=?",
        (row["order_id"], owner),
    ).fetchone()
    if (parent is None or row["proposed_amount_paise"] != parent["proposed_amount_paise"]
            or row["currency"] != parent["currency"]):
        raise StateStorageError("Invoice draft amount snapshot is inconsistent or unavailable.")
    return row


def _retry(conn: sqlite3.Connection, owner: str, key: str, digest: bytes) -> str | None:
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise InvoiceDraftError("Operation key was already used for a different request.")
    try:
        _owned(conn, owner, row["resource_id"])
    except AccessDenied as exc:
        raise StateStorageError("Stored operation result is unavailable.") from exc
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


class InvoiceDrafts:
    """Local draft snapshots only; no issuance, sending, collection or revenue."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, order_id: str, *, operation_key: str) -> str:
        order_id = _input(_record_id, order_id)
        key = _input(_key, operation_key)
        digest = _digest("invoice_draft_create", order_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _check_version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            order = conn.execute(
                "SELECT proposed_amount_paise, currency, status FROM order_drafts WHERE id=? AND owner_id=?",
                (order_id, owner),
            ).fetchone()
            if order is None:
                raise AccessDenied("Access denied.")
            if order["status"] != "draft":
                raise InvoiceDraftError("An active order draft is required.")
            record_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO invoice_drafts(id,owner_id,order_id,proposed_amount_paise,currency,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'draft',?,?)",
                (record_id, owner, order_id, order["proposed_amount_paise"], order["currency"], now, now),
            )
            _audit(conn, now, owner, "invoice_draft_created", record_id)
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
        digest = _digest("invoice_draft_cancel", record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _check_version(conn)
            row = _owned(conn, owner, record_id)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if row["status"] != "draft":
                raise InvoiceDraftError("Transition is not allowed.")
            conn.execute("UPDATE invoice_drafts SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?", (now, record_id, owner))
            _audit(conn, now, owner, "invoice_draft_cancelled", record_id)
            _remember(conn, owner, key, digest, record_id)
            return record_id
