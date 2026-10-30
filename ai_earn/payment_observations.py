"""Disposable unverified payment observations; never a payment or revenue.

No provider, external reference, customer data, verified or settled state is
supported. Trusted code must use a fixed auth/state pair; this is not a public
API or an OS boundary.
"""
import hmac
import secrets
import sqlite3

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError


_RESOURCE = "payment_observation"


class ObservationError(Exception):
    """Invalid input, conflicting retry, or disallowed local transition."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise ObservationError("Invalid observation input.") from exc


def _version(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] != 9:
        raise StateStorageError("State schema version 9 is required for payment observations.")


def _owned(conn, owner, ident):
    row = conn.execute("SELECT id, invoice_id, proposed_amount_paise, currency, status, created_at, updated_at "
                       "FROM payment_observations WHERE id=? AND owner_id=?", (ident, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    return row


def _retry(conn, owner, key, digest):
    row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise ObservationError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM payment_observations WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]


def _audit(conn, now, owner, event, ident):
    conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (now, owner, event, _RESOURCE, ident))


def _remember(conn, owner, key, digest, ident):
    conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
                 (owner, key, digest, _RESOURCE, ident))


class PaymentObservations:
    """Local flags for a proposed amount only; not a financial transaction."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, invoice_id: str, *, operation_key: str) -> str:
        invoice_id, key = _input(_record_id, invoice_id), _input(_key, operation_key)
        digest = _digest("payment_observation_create", invoice_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            invoice = conn.execute(
                "SELECT i.proposed_amount_paise, i.currency, i.status, o.status AS order_status "
                "FROM invoice_drafts i JOIN order_drafts o ON o.id=i.order_id AND o.owner_id=i.owner_id "
                "WHERE i.id=? AND i.owner_id=?", (invoice_id, owner),
            ).fetchone()
            if invoice is None:
                raise AccessDenied("Access denied.")
            if invoice["status"] != "draft" or invoice["order_status"] != "draft":
                raise ObservationError("Active invoice and order drafts are required.")
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO payment_observations VALUES (?,?,?,?,?,'unverified',?,?)",
                         (ident, owner, invoice_id, invoice["proposed_amount_paise"], invoice["currency"], now, now))
            _audit(conn, now, owner, "payment_observation_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict[str, str | int]:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            return dict(_owned(conn, owner, ident))

    def flag(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, operation_key, "flag")

    def unflag(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, operation_key, "unflag")

    def discard(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, operation_key, "discard")

    def _transition(self, token, record_id, operation_key, action):
        ident, key = _input(_record_id, record_id), _input(_key, operation_key)
        digest = _digest("payment_observation_" + action, ident)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if action == "flag" and row["status"] == "unverified":
                status = "flagged"
            elif action == "unflag" and row["status"] == "flagged":
                status = "unverified"
            elif action == "discard" and row["status"] in ("unverified", "flagged"):
                status = "discarded"
            else:
                raise ObservationError("Transition is not allowed.")
            conn.execute("UPDATE payment_observations SET status=?, updated_at=? WHERE id=? AND owner_id=?", (status, now, ident, owner))
            _audit(conn, now, owner, "payment_observation_" + action, ident)
            _remember(conn, owner, key, digest, ident)
            return ident
