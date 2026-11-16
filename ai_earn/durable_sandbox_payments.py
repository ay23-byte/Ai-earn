"""Durable synthetic payment rehearsal, never real money or provider evidence.

The caller must supply a disposable test key from trusted test composition.
This internal service has no network endpoint and does not authorize, verify,
initiate, settle or refund actual payments. Never use real/customer data.
"""
import hashlib
import hmac
import secrets

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _key, _record_id
from .payment_sandbox import SandboxPaymentError, _verify_synthetic_event
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError, _check_sandbox_payment_integrity, _financial_assertion_request, _SANDBOX_TRANSITIONS

_RESOURCE = "sandbox_payment"
_STAGE = {"confirmation": "sandbox_confirmed", "settlement": "sandbox_settled",
          "failure": "sandbox_failed", "refund": "sandbox_refunded"}


class DurableSandboxError(Exception):
    """Invalid synthetic input, key conflict or disallowed transition."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise DurableSandboxError("Invalid synthetic payment input.") from exc


def _version(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (14, 15, 16, 17, 18, 19, 20, 21):
        raise StateStorageError("State schema version 14 or 15 is required for durable synthetic payments.")


def _owned(conn, owner, ident):
    row = conn.execute("SELECT id,amount_paise,currency,key_fingerprint,stage,sequence,created_at,updated_at "
                       "FROM sandbox_payments WHERE id=? AND owner_id=?", (ident, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    _check_sandbox_payment_integrity(conn, owner, ident)
    return row


def _audit(conn, now, owner, event, ident):
    conn.execute("INSERT INTO state_audit(occurred_at,actor_id,event,resource_type,resource_id) VALUES(?,?,?,?,?)",
                 (now, owner, event, _RESOURCE, ident))


def _remember(conn, owner, key, digest, ident):
    conn.execute("INSERT INTO state_operations(owner_id,operation_key,request_hash,resource_type,resource_id) VALUES(?,?,?,?,?)",
                 (owner, key, digest, _RESOURCE, ident))


def _retry(conn, owner, key, digest):
    result = conn.execute("SELECT request_hash,resource_type,resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
                          (owner, key)).fetchone()
    if result is None:
        return None
    if result["resource_type"] != _RESOURCE or not hmac.compare_digest(result["request_hash"], digest):
        raise DurableSandboxError("Operation key was already used for a different request.")
    try:
        _owned(conn, owner, result["resource_id"])
    except AccessDenied as exc:
        raise StateStorageError("Stored operation result is unavailable.") from exc
    return result["resource_id"]


def _child_key():
    return "internal_sandbox_payment_" + secrets.token_hex(32)


def _view(row):
    values = dict(row)
    del values["key_fingerprint"]
    return {**values, "scope": "synthetic_only", "amount_kind": "not_revenue"}


class DurableSandboxPayments:
    """Session-owned test records and event retries, not actual finance."""

    def __init__(self, auth: AuthService, state: StateStorage, test_key: bytes):
        if type(test_key) is not bytes or not 32 <= len(test_key) <= 128:
            raise DurableSandboxError("Invalid synthetic test configuration.")
        self._auth, self._state, self._test_key = auth, state, test_key
        self._fingerprint = hashlib.sha256(test_key).digest()

    def create(self, token: str, amount_paise: int, *, operation_key: str) -> str:
        key = _input(_key, operation_key)
        if type(amount_paise) is not int or not 0 <= amount_paise <= 1_000_000_000_000:
            raise DurableSandboxError("Invalid synthetic amount.")
        digest = _financial_assertion_request("sandbox_payment_create", [amount_paise, self._fingerprint.hex()])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                row = _owned(conn, owner, existing)
                binding = _financial_assertion_request("sandbox_payment_binding", [key, existing, amount_paise, self._fingerprint.hex()])
                if (row["amount_paise"] != amount_paise or not hmac.compare_digest(row["key_fingerprint"], self._fingerprint) or conn.execute(
                    "SELECT 1 FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=? AND request_hash=?",
                    (owner, _RESOURCE, existing, binding)).fetchone() is None):
                    raise StateStorageError("Stored operation result is inconsistent.")
                return existing
            child = _child_key()
            if child == key or conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, child)).fetchone() is not None:
                raise StateStorageError("Internal operation key collision.")
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO sandbox_payments VALUES (?,?,?,'INR',?,'sandbox_awaiting',0,?,?)",
                         (ident, owner, amount_paise, self._fingerprint, now, now))
            _audit(conn, now, owner, "sandbox_payment_created", ident)
            _remember(conn, owner, key, digest, ident)
            _audit(conn, now, owner, "sandbox_payment_bound", ident)
            _remember(conn, owner, child, _financial_assertion_request("sandbox_payment_binding", [key, ident, amount_paise, self._fingerprint.hex()]), ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            return _view(_owned(conn, owner, ident))

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DurableSandboxError("Invalid page size.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            rows = conn.execute("SELECT id FROM sandbox_payments WHERE owner_id=? AND id>? ORDER BY id COLLATE BINARY LIMIT ?",
                                (owner, cursor, limit)).fetchall()
            return [_view(_owned(conn, owner, row["id"])) for row in rows]

    def _recheck_event(self, ident, amount, body, signature):
        try:
            _verify_synthetic_event(self._test_key, ident, amount, body, signature, int(self._auth.clock()))
        except SandboxPaymentError as exc:
            raise DurableSandboxError("Synthetic event freshness changed before commit.") from exc

    def accept(self, token: str, record_id: str, body: bytes, signature: str, *, operation_key: str) -> dict:
        """Apply one authenticated synthetic event; does not represent real finance."""
        ident, key = _input(_record_id, record_id), _input(_key, operation_key)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            if not hmac.compare_digest(row["key_fingerprint"], self._fingerprint):
                raise DurableSandboxError("Synthetic test configuration does not match.")
            try:
                event = _verify_synthetic_event(self._test_key, ident, row["amount_paise"], body, signature, now)
            except SandboxPaymentError as exc:
                raise DurableSandboxError("Invalid synthetic event.") from exc
            body_hash = hashlib.sha256(body).digest()
            digest = _financial_assertion_request("sandbox_payment_event", [ident, event["event_id"], body_hash.hex()])
            existing = _retry(conn, owner, key, digest)
            if existing is not None and existing != ident:
                raise StateStorageError("Stored operation result is inconsistent.")
            prior = conn.execute("SELECT payment_id,event_type,sequence,body_hash FROM sandbox_payment_events "
                                 "WHERE owner_id=? AND event_id=?", (owner, event["event_id"])).fetchone()
            if prior is not None:
                if (prior["payment_id"] != ident or prior["event_type"] != event["event_type"]
                        or prior["sequence"] != event["sequence"] or not hmac.compare_digest(prior["body_hash"], body_hash)):
                    raise DurableSandboxError("Conflicting synthetic event.")
                self._recheck_event(ident, row["amount_paise"], body, signature)
                return {"event_id": event["event_id"], "acknowledged_stage": _STAGE[prior["event_type"]], "scope": "synthetic_only"}
            if existing is not None:
                raise StateStorageError("Stored synthetic event is unavailable.")
            stage = _SANDBOX_TRANSITIONS.get((row["stage"], event["event_type"]))
            if stage is None or event["sequence"] != row["sequence"] + 1:
                raise DurableSandboxError("Synthetic transition is not allowed.")
            conn.execute("INSERT INTO sandbox_payment_events VALUES (?,?,?,?,?,?)",
                         (owner, event["event_id"], ident, event["event_type"], event["sequence"], body_hash))
            conn.execute("UPDATE sandbox_payments SET stage=?,sequence=?,updated_at=? WHERE id=? AND owner_id=?",
                         (stage, event["sequence"], now, ident, owner))
            _audit(conn, now, owner, "sandbox_payment_" + event["event_type"], ident)
            _remember(conn, owner, key, digest, ident)
            self._recheck_event(ident, row["amount_paise"], body, signature)
            return {"event_id": event["event_id"], "acknowledged_stage": stage, "scope": "synthetic_only"}
