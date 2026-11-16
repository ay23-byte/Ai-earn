"""Disposable, user-asserted financial stages; not verified transactions.

Every stage, including a claimed contract, invoice or initiation, is an
unverified local assertion. There is no confirmed/settled state, no ledger,
provider, customer data, financial action, or permission for external effects.
"""
import hmac
import json
import secrets

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id, _title
from .order_drafts import MAX_PROPOSED_PAISE
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError, _MULTI_STEP_HISTORIES, _check_financial_assertion_binding

_RESOURCE = "financial_assertion"
_NEXT = {"potential": "quote_asserted", "quote_asserted": "contract_asserted",
         "contract_asserted": "invoice_asserted", "invoice_asserted": "initiated_asserted"}
_HISTORY = next(transitions for _, _, kind, transitions in _MULTI_STEP_HISTORIES if kind == _RESOURCE)


class FinancialAssertionError(Exception):
    """Invalid input, conflicting key, or disallowed assertion transition."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise FinancialAssertionError("Invalid financial assertion input.") from exc


def _request(action, values):
    return _digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _version(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] not in (13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26):
        raise StateStorageError("State schema version 13, 14 or 15 is required for financial assertions.")


def _owned(conn, owner, ident):
    row = conn.execute("SELECT id,title,amount_paise,currency,stage,created_at,updated_at "
                       "FROM financial_assertions WHERE id=? AND owner_id=?", (ident, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? "
                          "AND resource_id=? ORDER BY id LIMIT 8", (owner, _RESOURCE, ident)).fetchall()
    if len(events) > 7:
        raise StateStorageError("Financial assertion history is inconsistent.")
    state = None
    for event in events:
        transition = (state, event[0])
        if transition not in _HISTORY:
            raise StateStorageError("Financial assertion history is inconsistent.")
        state = _HISTORY[transition]
    count = conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
                         (owner, _RESOURCE, ident)).fetchone()[0]
    if state != row["stage"] or sum(event[0] == "financial_assertion_bound" for event in events) != 1 or count != len(events):
        raise StateStorageError("Financial assertion history is inconsistent.")
    _check_financial_assertion_binding(conn, owner, ident, row["title"], row["amount_paise"])
    return row


def _view(row):
    return {**dict(row), "verification": "unverified", "amount_kind": "asserted_not_revenue"}


def _audit(conn, now, owner, event, ident):
    conn.execute("INSERT INTO state_audit(occurred_at,actor_id,event,resource_type,resource_id) VALUES (?,?,?,?,?)",
                 (now, owner, event, _RESOURCE, ident))


def _remember(conn, owner, key, digest, ident):
    conn.execute("INSERT INTO state_operations(owner_id,operation_key,request_hash,resource_type,resource_id) VALUES (?,?,?,?,?)",
                 (owner, key, digest, _RESOURCE, ident))


def _retry(conn, owner, key, digest):
    row = conn.execute("SELECT request_hash,resource_type,resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
                       (owner, key)).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise FinancialAssertionError("Operation key was already used for a different request.")
    try:
        _owned(conn, owner, row["resource_id"])
    except AccessDenied as exc:
        raise StateStorageError("Stored operation result is unavailable.") from exc
    return row["resource_id"]


def _child_key():
    return "internal_financial_assertion_" + secrets.token_hex(32)


class FinancialAssertions:
    """Unverified local assertions, never actual/confirmed/settled earnings."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth, self._state = auth, state

    def create(self, token: str, title: str, amount_paise: int, *, operation_key: str) -> str:
        title, key = _input(_title, title), _input(_key, operation_key)
        if type(amount_paise) is not int or not 0 <= amount_paise <= MAX_PROPOSED_PAISE:
            raise FinancialAssertionError("Invalid asserted amount.")
        digest = _request("financial_assertion_create", [title, amount_paise, "INR"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                row = _owned(conn, owner, existing)
                binding = _request("financial_assertion_create_binding", [key, existing, title, amount_paise])
                if (row["title"] != title or row["amount_paise"] != amount_paise
                        or conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=? AND request_hash=?",
                                        (owner, _RESOURCE, existing, binding)).fetchone() is None):
                    raise StateStorageError("Stored operation result is inconsistent.")
                return existing
            child = _child_key()
            if child == key or conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, child)).fetchone() is not None:
                raise StateStorageError("Internal operation key collision.")
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO financial_assertions VALUES (?,?,?,?,?,'potential',?,?)",
                         (ident, owner, title, amount_paise, "INR", now, now))
            _audit(conn, now, owner, "financial_assertion_created", ident)
            _remember(conn, owner, key, digest, ident)
            _audit(conn, now, owner, "financial_assertion_bound", ident)
            _remember(conn, owner, child, _request("financial_assertion_create_binding", [key, ident, title, amount_paise]), ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            return _view(_owned(conn, owner, ident))

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FinancialAssertionError("Invalid page size.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            rows = conn.execute("SELECT id FROM financial_assertions WHERE owner_id=? AND id>? ORDER BY id COLLATE BINARY LIMIT ?",
                                (owner, cursor, limit)).fetchall()
            return [_view(_owned(conn, owner, row["id"])) for row in rows]

    def advance(self, token: str, record_id: str, stage: str, *, confirmed: bool, operation_key: str) -> str:
        if stage not in ("quote_asserted", "contract_asserted", "invoice_asserted", "initiated_asserted"):
            raise FinancialAssertionError("Invalid asserted stage.")
        return self._transition(token, record_id, stage, confirmed, operation_key)

    def withdraw(self, token: str, record_id: str, *, confirmed: bool, operation_key: str) -> str:
        return self._transition(token, record_id, "withdrawn", confirmed, operation_key)

    def _transition(self, token, record_id, stage, confirmed, operation_key):
        ident, key = _input(_record_id, record_id), _input(_key, operation_key)
        if confirmed is not True:
            raise FinancialAssertionError("Explicit confirmation is required.")
        digest = _request("financial_assertion_transition", [ident, stage])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            if row["stage"] == "withdrawn" or (stage != "withdrawn" and _NEXT.get(row["stage"]) != stage):
                raise FinancialAssertionError("Transition is not allowed.")
            conn.execute("UPDATE financial_assertions SET stage=?,updated_at=? WHERE id=? AND owner_id=?", (stage, now, ident, owner))
            _audit(conn, now, owner, "financial_assertion_" + stage, ident)
            _remember(conn, owner, key, digest, ident)
            return ident
