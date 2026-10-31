"""Coordinated *draft-only* inspection and withdrawal, not financial action.

No invoice is issued, no payment is verified or cancelled, and no refund,
settlement, revenue, ledger or external effect is created. Use only disposable
data with a trusted fixed auth/state pair; this is not a public API or OS boundary.
"""
import secrets

from .auth import AccessDenied, AuthService
from .invoice_drafts import InvoiceDraftError, _audit as _invoice_audit, _owned as _owned_invoice, _remember as _invoice_remember, _retry as _invoice_retry
from .opportunity_drafts import DraftError, _digest, _key, _record_id
from .order_drafts import OrderError, _audit as _order_audit, _owned as _owned_order, _remember as _order_remember, _retry as _order_retry
from .payment_observations import _audit as _observation_audit, _owned as _owned_observation, _remember as _observation_remember, _version
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError, _check_invoice_withdraw_binding


# Bound total linked records so a withdrawal cannot silently truncate its scope
# or allocate an unbounded list of operations inside a write transaction.
MAX_LINKED_RECORDS = 100


class FinancialDraftError(Exception):
    """Invalid input, conflicting retry, excessive scope or disallowed action."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise FinancialDraftError("Invalid draft workflow input.") from exc


def _child_key():
    return "internal_financial_" + secrets.token_hex(32)


def _linked(conn, owner, order_id):
    """Load every bounded linked row and validate its owned amount snapshot."""
    invoice_ids = conn.execute(
        "SELECT id FROM invoice_drafts WHERE owner_id=? AND order_id=? ORDER BY id LIMIT ?",
        (owner, order_id, MAX_LINKED_RECORDS + 1),
    ).fetchall()
    if len(invoice_ids) > MAX_LINKED_RECORDS:
        raise FinancialDraftError("Too many linked draft records for one operation.")
    observation_ids = conn.execute(
        "SELECT p.id FROM payment_observations p JOIN invoice_drafts i "
        "ON p.invoice_id=i.id AND p.owner_id=i.owner_id "
        "WHERE i.owner_id=? AND i.order_id=? ORDER BY p.id LIMIT ?",
        (owner, order_id, MAX_LINKED_RECORDS + 1),
    ).fetchall()
    if len(invoice_ids) + len(observation_ids) > MAX_LINKED_RECORDS:
        raise FinancialDraftError("Too many linked draft records for one operation.")
    try:
        invoices = [_owned_invoice(conn, owner, row["id"]) for row in invoice_ids]
        observations = [_owned_observation(conn, owner, row["id"]) for row in observation_ids]
    except AccessDenied as exc:
        raise StateStorageError("Linked draft record is unavailable.") from exc
    return invoices, observations


class FinancialDraftWorkflows:
    """Local estimates and placeholders only; no financial truth or effect."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def inspect(self, token: str, order_id: str) -> dict:
        """Return a bounded point-in-time view, never a payment/revenue total."""
        ident = _input(_record_id, order_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            order = _owned_order(conn, owner, ident)
            invoices, observations = _linked(conn, owner, ident)
            return {"observed_at": now, "order": dict(order),
                    "invoices": [dict(item) for item in invoices],
                    "observations": [dict(item) for item in observations]}

    def withdraw_invoice(self, token: str, invoice_id: str, *, confirmed: bool, operation_key: str) -> str:
        """Cancel one local invoice draft and discard its placeholders atomically.

        This neither cancels an issued invoice nor performs a payment action.
        Retries acknowledge historical execution, not the current external world.
        Independent legacy APIs may still leave partial trees.
        """
        ident, key = _input(_record_id, invoice_id), _input(_key, operation_key)
        if confirmed is not True:
            raise FinancialDraftError("Explicit confirmation is required.")
        digest = _digest("financial_invoice_withdraw", ident)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            invoice = _owned_invoice(conn, owner, ident)
            _check_invoice_withdraw_binding(conn, owner, ident)
            rows = conn.execute("SELECT id FROM payment_observations WHERE owner_id=? AND invoice_id=? ORDER BY id LIMIT ?",
                                (owner, ident, MAX_LINKED_RECORDS + 1)).fetchall()
            if len(rows) > MAX_LINKED_RECORDS:
                raise FinancialDraftError("Too many linked draft records for one operation.")
            try:
                observations = [_owned_observation(conn, owner, row["id"]) for row in rows]
            except AccessDenied as exc:
                raise StateStorageError("Linked draft record is unavailable.") from exc
            try:
                existing = _invoice_retry(conn, owner, key, digest)
            except InvoiceDraftError as exc:
                raise FinancialDraftError("Operation key was already used for a different request.") from exc
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            active = [row for row in observations if row["status"] in ("unverified", "flagged")]
            cancel_invoice = invoice["status"] == "draft"
            count = len(active) + int(cancel_invoice)
            if not count:
                raise FinancialDraftError("No active linked drafts to withdraw.")
            child_keys = [_child_key() for _ in range(count)]
            if (key in child_keys or len(set(child_keys)) != len(child_keys)
                    or any(conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, child)).fetchone()
                           is not None for child in child_keys)):
                raise StateStorageError("Internal operation key collision.")
            keys = iter(child_keys)
            for item in active:
                record_id = item["id"]
                conn.execute("UPDATE payment_observations SET status='discarded', updated_at=? WHERE id=? AND owner_id=?", (now, record_id, owner))
                _observation_audit(conn, now, owner, "payment_observation_discard", record_id)
                _observation_remember(conn, owner, next(keys), _digest("payment_observation_discard", record_id), record_id)
            if cancel_invoice:
                conn.execute("UPDATE invoice_drafts SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
                _invoice_audit(conn, now, owner, "invoice_draft_cancelled", ident)
                _invoice_remember(conn, owner, next(keys), _digest("invoice_draft_cancel", ident), ident)
            _invoice_audit(conn, now, owner, "financial_invoice_withdrawn", ident)
            _invoice_remember(conn, owner, key, digest, ident)
            return ident

    def withdraw(self, token: str, order_id: str, *, confirmed: bool, operation_key: str) -> str:
        """Atomically cancel/discard all linked local drafts, not outside effects.

        Exact retries acknowledge the original transaction. An already-cancelled
        parent can be cleaned up, but a new key with nothing to change is refused.
        Independent direct APIs remain available and may leave partial trees.
        """
        ident, key = _input(_record_id, order_id), _input(_key, operation_key)
        if confirmed is not True:
            raise FinancialDraftError("Explicit confirmation is required.")
        digest = _digest("financial_drafts_withdraw", ident)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            order = _owned_order(conn, owner, ident)
            invoices, observations = _linked(conn, owner, ident)
            try:
                existing = _order_retry(conn, owner, key, digest)
            except OrderError as exc:
                raise FinancialDraftError("Operation key was already used for a different request.") from exc
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            active_observations = [item for item in observations if item["status"] in ("unverified", "flagged")]
            active_invoices = [item for item in invoices if item["status"] == "draft"]
            active_order = order["status"] == "draft"
            count = len(active_observations) + len(active_invoices) + int(active_order)
            if not count:
                raise FinancialDraftError("No active linked drafts to withdraw.")
            child_keys = [_child_key() for _ in range(count)]
            if (key in child_keys or len(set(child_keys)) != len(child_keys)
                    or any(conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, child)).fetchone()
                           is not None for child in child_keys)):
                raise StateStorageError("Internal operation key collision.")
            keys = iter(child_keys)
            for item in active_observations:
                record_id = item["id"]
                conn.execute("UPDATE payment_observations SET status='discarded', updated_at=? WHERE id=? AND owner_id=?", (now, record_id, owner))
                _observation_audit(conn, now, owner, "payment_observation_discard", record_id)
                _observation_remember(conn, owner, next(keys), _digest("payment_observation_discard", record_id), record_id)
            for item in active_invoices:
                record_id = item["id"]
                conn.execute("UPDATE invoice_drafts SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?", (now, record_id, owner))
                _invoice_audit(conn, now, owner, "invoice_draft_cancelled", record_id)
                _invoice_remember(conn, owner, next(keys), _digest("invoice_draft_cancel", record_id), record_id)
            if active_order:
                conn.execute("UPDATE order_drafts SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
                _order_audit(conn, now, owner, "order_draft_cancelled", ident)
                _order_remember(conn, owner, next(keys), _digest("order_draft_cancel", ident), ident)
            _order_audit(conn, now, owner, "financial_drafts_withdrawn", ident)
            _order_remember(conn, owner, key, digest, ident)
            return ident
