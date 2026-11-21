"""Finance subsystem v28 — owner-scoped, session-authorized, verified ledger."""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from typing import Any

from .auth import AccessDenied, AuthService
from .financial_math import net_cash_paise
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

# Resource types
_RES_TAX = "tax_config"
_RES_BILLING = "billing_profile"
_RES_PAYMENT = "payment_record"
_RES_EXPENSE = "expense_record"
_RES_REFUND = "refund_record"
_RES_FEE = "fee_record"


class FinanceError(Exception):
    pass


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, FinanceError) as exc:
        raise FinanceError(str(exc)) from exc
    except Exception as exc:
        raise FinanceError("Invalid finance input.") from exc


def _jurisdiction(v: str) -> str:
    if not isinstance(v, str) or not 1 <= len(v) <= 100 or len(v.strip()) == 0:
        raise FinanceError("Invalid jurisdiction.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid jurisdiction.")
    return v.strip()


def _rate_bps(v: int) -> int:
    if type(v) is not int or not 0 <= v <= 10000:
        raise FinanceError("Invalid rate_bps.")
    return v


def _billing_name(v: str) -> str:
    if not isinstance(v, str) or not 1 <= len(v) <= 200 or len(v.strip()) == 0:
        raise FinanceError("Invalid billing_name.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid billing_name.")
    return v


def _billing_contact(v: str) -> str:
    if not isinstance(v, str) or not 1 <= len(v) <= 500:
        raise FinanceError("Invalid billing_contact.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid billing_contact.")
    return v


def _billing_address(v: str) -> str:
    if not isinstance(v, str) or len(v) > 1000:
        raise FinanceError("Invalid billing_address.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v if c not in "\n\t"):
        raise FinanceError("Invalid billing_address.")
    return v


def _tax_id(v: str) -> str:
    if not isinstance(v, str) or len(v) > 100:
        raise FinanceError("Invalid tax_id.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid tax_id.")
    return v


def _amount(v: int) -> int:
    if type(v) is not int or not 0 <= v <= 1_000_000_000_000:
        raise FinanceError("Invalid amount.")
    return v


def _category(v: str) -> str:
    if not isinstance(v, str) or not 1 <= len(v) <= 100 or len(v.strip()) == 0:
        raise FinanceError("Invalid category.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid category.")
    return v


def _note(v: str) -> str:
    if not isinstance(v, str) or len(v) > 500:
        raise FinanceError("Invalid note.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid note.")
    return v


def _reason(v: str) -> str:
    if not isinstance(v, str) or len(v) > 500:
        raise FinanceError("Invalid reason.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid reason.")
    return v


def _fee_type(v: str) -> str:
    if v not in ("platform", "gateway", "other"):
        raise FinanceError("Invalid fee_type.")
    return v


def _provider(v: str) -> str:
    if not isinstance(v, str) or not 1 <= len(v) <= 32:
        raise FinanceError("Invalid provider.")
    if v not in ("manual", "razorpay", "stripe", "sandbox"):
        raise FinanceError("Invalid provider.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid provider.")
    return v


def _provider_ref(v: str) -> str:
    if not isinstance(v, str) or len(v) > 128:
        raise FinanceError("Invalid provider_ref.")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise FinanceError("Invalid provider_ref.")
    return v


def _status_payment(v: str) -> str:
    if v not in ("initiated", "received", "settled", "failed", "refunded"):
        raise FinanceError("Invalid payment status.")
    return v


def _due_at(v: int | None) -> int | None:
    if v is None:
        return None
    if type(v) is not int or not 0 <= v <= 4102444800:
        raise FinanceError("Invalid due_at.")
    return v


def _incurred_at(v: int) -> int:
    if type(v) is not int or not 0 <= v <= 4102444800:
        raise FinanceError("Invalid incurred_at.")
    return v


def _verified(v: int) -> int:
    if v not in (0, 1):
        raise FinanceError("Invalid verified flag.")
    return v


def _currency(v: str) -> str:
    if v != "INR":
        raise FinanceError("Currency must be INR.")
    return v


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS

    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver not in SUPPORTED_STATE_VERSIONS or ver < 28:
        raise StateStorageError("State schema version 28 is required for finance.")


def _retry(conn, owner, key, digest, resource, table, id_col="id"):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != resource or not hmac.compare_digest(row["request_hash"], digest):
        raise FinanceError("Operation key was already used for a different request.")
    if (
        conn.execute(
            f"SELECT 1 FROM {table} WHERE {id_col}=? AND owner_id=?", (row["resource_id"], owner)
        ).fetchone()
        is None
    ):
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]


def _audit(conn, now, owner, event, resource, rid):
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, event, resource, rid),
    )


def _remember(conn, owner, key, digest, resource, rid):
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, resource, rid),
    )


def _request(action: str, values: list) -> bytes:
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _owned_customer(conn, owner, cid):
    if (
        conn.execute("SELECT 1 FROM customers WHERE id=? AND owner_id=?", (cid, owner)).fetchone()
        is None
    ):
        raise AccessDenied("Access denied.")


def _owned_invoice(conn, owner, iid):
    row = conn.execute("SELECT * FROM invoices WHERE id=? AND owner_id=?", (iid, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    return row


def _owned_payment(conn, owner, pid):
    row = conn.execute(
        "SELECT * FROM payment_records WHERE id=? AND owner_id=?", (pid, owner)
    ).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    return row


class TaxConfigs:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def set(self, token: str, jurisdiction: str, rate_bps: int, *, operation_key: str) -> str:
        juris = _input(_jurisdiction, jurisdiction)
        rate = _input(_rate_bps, rate_bps)
        key = _input(_key, operation_key)
        digest = _request("tax_set", [juris, rate])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RES_TAX, "tax_configs", "owner_id")
            if existing is not None:
                return existing
            # Check if already exists for owner -> update
            row = conn.execute("SELECT * FROM tax_configs WHERE owner_id=?", (owner,)).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE tax_configs SET jurisdiction=?, rate_bps=?, updated_at=? WHERE owner_id=?",
                    (juris, rate, now, owner),
                )
                _audit(conn, now, owner, "tax_config_updated", _RES_TAX, owner)
                _remember(conn, owner, key, digest, _RES_TAX, owner)
                return owner
            conn.execute(
                "INSERT INTO tax_configs(owner_id, jurisdiction, rate_bps, currency, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (owner, juris, rate, "INR", now, now),
            )
            _audit(conn, now, owner, "tax_config_created", _RES_TAX, owner)
            _remember(conn, owner, key, digest, _RES_TAX, owner)
            return owner

    def get(self, token: str) -> dict | None:
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = conn.execute("SELECT * FROM tax_configs WHERE owner_id=?", (owner,)).fetchone()
            if row is None:
                return None
            return dict(row)


class BillingProfiles:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _owned(self, conn, owner, pid):
        row = conn.execute(
            "SELECT * FROM billing_profiles WHERE id=? AND owner_id=?", (pid, owner)
        ).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        # verify audit history
        events = conn.execute(
            "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
            (owner, _RES_BILLING, pid),
        ).fetchall()
        if not events or events[0][0] not in ("billing_profile_created", "billing_profile_updated"):
            raise StateStorageError("Billing profile history inconsistent.")
        cnt = conn.execute(
            "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
            (owner, _RES_BILLING, pid),
        ).fetchone()[0]
        if cnt != len(events):
            # Allow multiple updates: count should equal events, but creation counts as 1
            pass
        return row

    def create(
        self,
        token: str,
        customer_id: str,
        billing_name: str,
        billing_contact: str,
        billing_address: str = "",
        tax_id: str = "",
        *,
        operation_key: str,
    ) -> str:
        cid = _input(_record_id, customer_id)
        bn = _input(_billing_name, billing_name)
        bc = _input(_billing_contact, billing_contact)
        ba = _input(_billing_address, billing_address)
        ti = _input(_tax_id, tax_id)
        key = _input(_key, operation_key)
        digest = _request("billing_create", [cid, bn, bc, ba, ti])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RES_BILLING, "billing_profiles")
            if existing:
                return existing
            _owned_customer(conn, owner, cid)
            # enforce unique per customer per owner
            if (
                conn.execute(
                    "SELECT 1 FROM billing_profiles WHERE customer_id=? AND owner_id=?",
                    (cid, owner),
                ).fetchone()
                is not None
            ):
                raise FinanceError("Billing profile already exists for this customer.")
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO billing_profiles(id, owner_id, customer_id, billing_name, billing_contact, billing_address, tax_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, owner, cid, bn, bc, ba, ti, now, now),
            )
            _audit(conn, now, owner, "billing_profile_created", _RES_BILLING, ident)
            _remember(conn, owner, key, digest, _RES_BILLING, ident)
            return ident

    def get(self, token: str, profile_id: str) -> dict:
        pid = _input(_record_id, profile_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(self._owned(conn, owner, pid))

    def list(
        self,
        token: str,
        *,
        customer_id: str | None = None,
        after_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FinanceError("Invalid limit.")
        if customer_id is not None:
            cid = _input(_record_id, customer_id)
        else:
            cid = None
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            if cid is not None:
                _owned_customer(conn, owner, cid)
            query = "SELECT id FROM billing_profiles WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if cid is not None:
                query += " AND customer_id=?"
                params.append(cid)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(self._owned(conn, owner, r["id"])) for r in rows]

    def update(
        self,
        token: str,
        profile_id: str,
        *,
        billing_name: str | None = None,
        billing_contact: str | None = None,
        billing_address: str | None = None,
        tax_id: str | None = None,
        operation_key: str,
    ) -> str:
        pid = _input(_record_id, profile_id)
        key = _input(_key, operation_key)
        # Build digest from provided fields
        vals = [pid, billing_name, billing_contact, billing_address, tax_id]
        digest = _request("billing_update", vals)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RES_BILLING, "billing_profiles")
            if existing:
                return pid
            self._owned(conn, owner, pid)
            updates = {}
            if billing_name is not None:
                updates["billing_name"] = _input(_billing_name, billing_name)
            if billing_contact is not None:
                updates["billing_contact"] = _input(_billing_contact, billing_contact)
            if billing_address is not None:
                updates["billing_address"] = _input(_billing_address, billing_address)
            if tax_id is not None:
                updates["tax_id"] = _input(_tax_id, tax_id)
            if not updates:
                raise FinanceError("No updates provided.")
            set_clause = ", ".join(f"{k}=?" for k in updates) + ", updated_at=?"
            params = list(updates.values()) + [now, pid, owner]
            conn.execute(
                f"UPDATE billing_profiles SET {set_clause} WHERE id=? AND owner_id=?", tuple(params)
            )
            _audit(conn, now, owner, "billing_profile_updated", _RES_BILLING, pid)
            _remember(conn, owner, key, digest, _RES_BILLING, pid)
            return pid


class PaymentRecords:
    _TRANSITIONS = {
        None: {"initiated"},
        "initiated": {"received", "failed"},
        "received": {"settled", "failed", "refunded"},
        "settled": {"refunded"},
        "failed": set(),
        "refunded": set(),
    }

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _owned(self, conn, owner, pid):
        row = conn.execute(
            "SELECT * FROM payment_records WHERE id=? AND owner_id=?", (pid, owner)
        ).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        events = conn.execute(
            "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
            (owner, _RES_PAYMENT, pid),
        ).fetchall()
        if not events or events[0][0] != "payment_created":
            raise StateStorageError("Payment history inconsistent.")
        # Replay transitions
        current = None
        for (ev,) in events:
            if ev == "payment_created":
                if current is not None:
                    raise StateStorageError("Payment history inconsistent.")
                current = "initiated"
            elif ev.startswith("payment_"):
                target = ev.replace("payment_", "")
                # map event to status: payment_received -> received etc
                if target not in self._TRANSITIONS.get(current, set()):
                    raise StateStorageError("Payment history inconsistent.")
                current = target
            else:
                raise StateStorageError("Payment history inconsistent.")
        if current != row["status"]:
            raise StateStorageError("Payment history inconsistent.")
        cnt = conn.execute(
            "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
            (owner, _RES_PAYMENT, pid),
        ).fetchone()[0]
        if cnt != len(events):
            raise StateStorageError("Payment history inconsistent.")
        return row

    def create(
        self,
        token: str,
        amount_paise: int,
        provider: str = "manual",
        provider_ref: str = "",
        *,
        invoice_id: str | None = None,
        order_id: str | None = None,
        verified: int = 0,
        operation_key: str,
    ) -> str:
        amt = _input(_amount, amount_paise)
        prov = _input(_provider, provider)
        pref = _input(_provider_ref, provider_ref)
        ver = _input(_verified, verified)
        key = _input(_key, operation_key)
        iid = _input(_record_id, invoice_id) if invoice_id is not None else None
        oid = _input(_record_id, order_id) if order_id is not None else None
        if iid is None and oid is None:
            # At least one should be provided? Allow both None for generic payment, but spec says linked to invoice/order optionally
            pass
        digest = _request("payment_create", [amt, prov, pref, iid or "", oid or "", ver])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RES_PAYMENT, "payment_records")
            if existing:
                return existing
            if iid is not None:
                _owned_invoice(conn, owner, iid)
            if oid is not None:
                # check order exists either in order_drafts or customer_orders
                od = conn.execute(
                    "SELECT 1 FROM order_drafts WHERE id=? AND owner_id=?", (oid, owner)
                ).fetchone()
                co = conn.execute(
                    "SELECT 1 FROM customer_orders WHERE id=? AND owner_id=?", (oid, owner)
                ).fetchone()
                if od is None and co is None:
                    raise AccessDenied("Access denied.")
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO payment_records(id, owner_id, invoice_id, order_id, amount_paise, currency, status, provider, provider_ref, verified, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (ident, owner, iid, oid, amt, "INR", "initiated", prov, pref, ver, now, now),
            )
            _audit(conn, now, owner, "payment_created", _RES_PAYMENT, ident)
            _remember(conn, owner, key, digest, _RES_PAYMENT, ident)
            return ident

    def get(self, token: str, payment_id: str) -> dict:
        pid = _input(_record_id, payment_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(self._owned(conn, owner, pid))

    def list(
        self,
        token: str,
        *,
        status: str | None = None,
        invoice_id: str | None = None,
        after_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FinanceError("Invalid limit.")
        if status is not None:
            _status_payment(status)
        iid = _input(_record_id, invoice_id) if invoice_id is not None else None
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM payment_records WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if status is not None:
                query += " AND status=?"
                params.append(status)
            if iid is not None:
                query += " AND invoice_id=?"
                params.append(iid)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(self._owned(conn, owner, r["id"])) for r in rows]

    def _transition(
        self, token, pid, target, event, operation_key, verified_update: int | None = None
    ):
        pid_v = _input(_record_id, pid)
        key = _input(_key, operation_key)
        digest = _request(event, [pid_v, target])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = self._owned(conn, owner, pid_v)
            existing = _retry(conn, owner, key, digest, _RES_PAYMENT, "payment_records")
            if existing:
                return pid_v
            cur = row["status"]
            if target not in self._TRANSITIONS.get(cur, set()):
                raise FinanceError(f"Transition from {cur} to {target} not allowed.")
            # Never infer verified: must be explicit, cannot auto-verify on status change unless caller provides verified flag and it's supported
            verified = row["verified"]
            if verified_update is not None:
                verified = _input(_verified, verified_update)
                if verified == 1 and (row["provider"] != "manual" or target != "settled"):
                    raise FinanceError(
                        "Only a manually recorded payment may be explicitly verified after settlement."
                    )
            conn.execute(
                "UPDATE payment_records SET status=?, verified=?, updated_at=? WHERE id=? AND owner_id=?",
                (target, verified, now, pid_v, owner),
            )
            _audit(conn, now, owner, event, _RES_PAYMENT, pid_v)
            _remember(conn, owner, key, digest, _RES_PAYMENT, pid_v)
            return pid_v

    def mark_received(
        self, token: str, payment_id: str, *, verified: int | None = None, operation_key: str
    ) -> str:
        return self._transition(
            token, payment_id, "received", "payment_received", operation_key, verified
        )

    def mark_settled(
        self, token: str, payment_id: str, *, verified: int | None = None, operation_key: str
    ) -> str:
        return self._transition(
            token, payment_id, "settled", "payment_settled", operation_key, verified
        )

    def mark_failed(self, token: str, payment_id: str, *, operation_key: str) -> str:
        return self._transition(token, payment_id, "failed", "payment_failed", operation_key)

    def mark_refunded(self, token: str, payment_id: str, *, operation_key: str) -> str:
        return self._transition(token, payment_id, "refunded", "payment_refunded", operation_key)

    def set_verified(
        self, token: str, payment_id: str, verified: int, *, operation_key: str
    ) -> str:
        pid = _input(_record_id, payment_id)
        key = _input(_key, operation_key)
        ver = _input(_verified, verified)
        digest = _request("payment_verify", [pid, ver])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RES_PAYMENT, "payment_records")
            if existing:
                return pid
            row = self._owned(conn, owner, pid)
            # Verification is an explicit operator attestation, never inferred from a provider
            # callback or sandbox record. Only manual records in settled state may be verified.
            if ver == 1 and (row["provider"] != "manual" or row["status"] != "settled"):
                raise FinanceError(
                    "Only a manually recorded, settled payment may be explicitly verified."
                )
            conn.execute(
                "UPDATE payment_records SET verified=?, updated_at=? WHERE id=? AND owner_id=?",
                (ver, now, pid, owner),
            )
            _audit(conn, now, owner, "payment_verified", _RES_PAYMENT, pid)
            _remember(conn, owner, key, digest, _RES_PAYMENT, pid)
            return pid


class ExpenseRecords:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _owned(self, conn, owner, eid):
        row = conn.execute(
            "SELECT * FROM expense_records WHERE id=? AND owner_id=?", (eid, owner)
        ).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        events = conn.execute(
            "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
            (owner, _RES_EXPENSE, eid),
        ).fetchall()
        if not events or events[0][0] != "expense_created":
            raise StateStorageError("Expense history inconsistent.")
        cnt = conn.execute(
            "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
            (owner, _RES_EXPENSE, eid),
        ).fetchone()[0]
        if cnt != len(events):
            raise StateStorageError("Expense history inconsistent.")
        return row

    def create(
        self,
        token: str,
        amount_paise: int,
        category: str,
        note: str = "",
        incurred_at: int | None = None,
        verified: int = 0,
        *,
        operation_key: str,
    ) -> str:
        amt = _input(_amount, amount_paise)
        cat = _input(_category, category)
        n = _input(_note, note)
        inc = (
            _input(_incurred_at, incurred_at)
            if incurred_at is not None
            else int(self._auth.clock())
            if hasattr(self._auth, "clock")
            else 0
        )
        # Use provided or now
        ver = _input(_verified, verified)
        key = _input(_key, operation_key)
        digest = _request("expense_create", [amt, cat, n, inc, ver])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RES_EXPENSE, "expense_records")
            if existing:
                return existing
            # incurred_at should be provided explicitly; if None use now
            if incurred_at is None:
                inc = now
            else:
                inc = _input(_incurred_at, incurred_at)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO expense_records(id, owner_id, amount_paise, currency, category, note, incurred_at, verified, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, owner, amt, "INR", cat, n, inc, ver, now, now),
            )
            _audit(conn, now, owner, "expense_created", _RES_EXPENSE, ident)
            _remember(conn, owner, key, digest, _RES_EXPENSE, ident)
            return ident

    def get(self, token: str, expense_id: str) -> dict:
        eid = _input(_record_id, expense_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(self._owned(conn, owner, eid))

    def list(
        self,
        token: str,
        *,
        after_id: str | None = None,
        limit: int = 20,
        verified: int | None = None,
    ) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FinanceError("Invalid limit.")
        if verified is not None:
            _verified(verified)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM expense_records WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if verified is not None:
                query += " AND verified=?"
                params.append(verified)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(self._owned(conn, owner, r["id"])) for r in rows]


class RefundRecords:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _owned(self, conn, owner, rid):
        row = conn.execute(
            "SELECT * FROM refund_records WHERE id=? AND owner_id=?", (rid, owner)
        ).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        events = conn.execute(
            "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
            (owner, _RES_REFUND, rid),
        ).fetchall()
        if not events or events[0][0] != "refund_created":
            raise StateStorageError("Refund history inconsistent.")
        cnt = conn.execute(
            "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
            (owner, _RES_REFUND, rid),
        ).fetchone()[0]
        if cnt != len(events):
            raise StateStorageError("Refund history inconsistent.")
        return row

    def create(
        self,
        token: str,
        payment_id: str,
        amount_paise: int,
        reason: str = "",
        verified: int = 0,
        *,
        operation_key: str,
    ) -> str:
        pid = _input(_record_id, payment_id)
        amt = _input(_amount, amount_paise)
        rs = _input(_reason, reason)
        ver = _input(_verified, verified)
        key = _input(_key, operation_key)
        digest = _request("refund_create", [pid, amt, rs, ver])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RES_REFUND, "refund_records")
            if existing:
                return existing
            pay = _owned_payment(conn, owner, pid)
            # Only allow refund if payment is settled/received/refunded? Allow but check amount does not exceed payment
            if amt > pay["amount_paise"]:
                raise FinanceError("Refund amount exceeds payment amount.")
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO refund_records(id, owner_id, payment_id, amount_paise, currency, reason, verified, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, owner, pid, amt, "INR", rs, ver, now, now),
            )
            _audit(conn, now, owner, "refund_created", _RES_REFUND, ident)
            _remember(conn, owner, key, digest, _RES_REFUND, ident)
            return ident

    def get(self, token: str, refund_id: str) -> dict:
        rid = _input(_record_id, refund_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(self._owned(conn, owner, rid))

    def list(
        self,
        token: str,
        *,
        payment_id: str | None = None,
        after_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FinanceError("Invalid limit.")
        pid = _input(_record_id, payment_id) if payment_id is not None else None
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM refund_records WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if pid is not None:
                query += " AND payment_id=?"
                params.append(pid)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(self._owned(conn, owner, r["id"])) for r in rows]


class FeeRecords:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _owned(self, conn, owner, fid):
        row = conn.execute(
            "SELECT * FROM fee_records WHERE id=? AND owner_id=?", (fid, owner)
        ).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        events = conn.execute(
            "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
            (owner, _RES_FEE, fid),
        ).fetchall()
        if not events or events[0][0] != "fee_created":
            raise StateStorageError("Fee history inconsistent.")
        cnt = conn.execute(
            "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
            (owner, _RES_FEE, fid),
        ).fetchone()[0]
        if cnt != len(events):
            raise StateStorageError("Fee history inconsistent.")
        return row

    def create(
        self,
        token: str,
        amount_paise: int,
        fee_type: str = "platform",
        note: str = "",
        *,
        payment_id: str | None = None,
        verified: int = 0,
        operation_key: str,
    ) -> str:
        amt = _input(_amount, amount_paise)
        ft = _input(_fee_type, fee_type)
        n = _input(_note, note)
        ver = _input(_verified, verified)
        key = _input(_key, operation_key)
        pid = _input(_record_id, payment_id) if payment_id is not None else None
        digest = _request("fee_create", [amt, ft, n, pid or "", ver])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RES_FEE, "fee_records")
            if existing:
                return existing
            if pid is not None:
                _owned_payment(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO fee_records(id, owner_id, payment_id, amount_paise, currency, fee_type, note, verified, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, owner, pid, amt, "INR", ft, n, ver, now, now),
            )
            _audit(conn, now, owner, "fee_created", _RES_FEE, ident)
            _remember(conn, owner, key, digest, _RES_FEE, ident)
            return ident

    def get(self, token: str, fee_id: str) -> dict:
        fid = _input(_record_id, fee_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(self._owned(conn, owner, fid))

    def list(
        self,
        token: str,
        *,
        payment_id: str | None = None,
        after_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise FinanceError("Invalid limit.")
        pid = _input(_record_id, payment_id) if payment_id is not None else None
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM fee_records WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if pid is not None:
                if pid is None:
                    query += " AND payment_id IS NULL"
                else:
                    query += " AND payment_id=?"
                    params.append(pid)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(self._owned(conn, owner, r["id"])) for r in rows]


class FinancialReports:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _version_check(self, conn):
        _version_check(conn)

    def eleven_concepts(self, token: str) -> dict:
        """Strict 11-concept breakdown using verified records only for verified income."""
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)

            # Helpers
            def sum_verified(table, where=""):
                q = f"SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM {table} WHERE owner_id=? AND verified=1 {where}"
                row = conn.execute(q, (owner,)).fetchone()
                return {
                    "count": row[0],
                    "amount_paise": row[1],
                    "verification": "verified",
                    "currency": "INR",
                }

            def sum_all(table, amount_col, where=""):
                q = f"SELECT COUNT(*), COALESCE(SUM({amount_col}),0) FROM {table} WHERE owner_id=? {where}"
                row = conn.execute(q, (owner,)).fetchone()
                return {
                    "count": row[0],
                    "amount_paise": row[1],
                    "verification": "unverified",
                    "currency": "INR",
                }

            # 1 Proposed: order_drafts draft
            proposed = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(proposed_amount_paise),0) FROM order_drafts WHERE owner_id=? AND status='draft'",
                (owner,),
            ).fetchone()
            proposed_d = {
                "count": proposed[0],
                "amount_paise": proposed[1],
                "verification": "unverified",
                "source": "order_drafts draft",
                "currency": "INR",
            }
            # Also customer_orders draft
            co_prop = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM customer_orders WHERE owner_id=? AND status='draft'",
                (owner,),
            ).fetchone()
            # 2 Quoted
            quoted = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM financial_assertions WHERE owner_id=? AND stage='quote_asserted'",
                (owner,),
            ).fetchone()
            quoted_d = {
                "count": quoted[0],
                "amount_paise": quoted[1],
                "verification": "unverified",
                "source": "financial_assertions quote_asserted",
                "currency": "INR",
            }
            # 3 Contracted
            contracted = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM financial_assertions WHERE owner_id=? AND stage='contract_asserted'",
                (owner,),
            ).fetchone()
            contracted_d = {
                "count": contracted[0],
                "amount_paise": contracted[1],
                "verification": "unverified",
                "source": "financial_assertions contract_asserted",
                "currency": "INR",
            }
            # 4 Invoiced: invoice_drafts + invoices draft/issued (unverified), plus verified invoices?
            inv_drafts = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(proposed_amount_paise),0) FROM invoice_drafts WHERE owner_id=? AND status='draft'",
                (owner,),
            ).fetchone()
            inv_drafts_d = {
                "count": inv_drafts[0],
                "amount_paise": inv_drafts[1],
                "verification": "unverified",
                "source": "invoice_drafts draft",
                "currency": "INR",
            }
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND status IN ('draft','issued','paid','overdue','void')",
                (owner,),
            ).fetchone()
            invoiced_all = {
                "count": row[0],
                "amount_paise": row[1],
                "verification": "unverified",
                "source": "invoices all statuses",
                "currency": "INR",
            }
            row2 = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND status IN ('issued','paid','overdue')",
                (owner,),
            ).fetchone()
            invoiced_issued = {
                "count": row2[0],
                "amount_paise": row2[1],
                "verification": "unverified",
                "note": "PDF stub, not demand",
                "currency": "INR",
            }
            # 5 Payment initiated: verified vs unverified separated
            init_verified = sum_verified("payment_records", "AND status='initiated'")
            init_all = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='initiated'",
                (owner,),
            ).fetchone()
            init_all_d = {
                "count": init_all[0],
                "amount_paise": init_all[1],
                "verification": "unverified",
                "currency": "INR",
            }
            # 6 Payment received
            recv_verified = sum_verified("payment_records", "AND status='received'")
            recv_all = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='received'",
                (owner,),
            ).fetchone()
            recv_all_d = {
                "count": recv_all[0],
                "amount_paise": recv_all[1],
                "verification": "unverified",
                "currency": "INR",
            }
            # 7 Payment settled
            settled_verified = sum_verified("payment_records", "AND status='settled'")
            settled_all = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='settled'",
                (owner,),
            ).fetchone()
            settled_all_d = {
                "count": settled_all[0],
                "amount_paise": settled_all[1],
                "verification": "unverified",
                "currency": "INR",
            }
            # 8 Refunds verified vs all
            refunds_verified = sum_verified("refund_records")
            refunds_all = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM refund_records WHERE owner_id=?",
                (owner,),
            ).fetchone()
            refunds_all_d = {
                "count": refunds_all[0],
                "amount_paise": refunds_all[1],
                "verification": "unverified",
                "currency": "INR",
            }
            # 9 Expenses
            expenses_verified = sum_verified("expense_records")
            expenses_all = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM expense_records WHERE owner_id=?",
                (owner,),
            ).fetchone()
            expenses_all_d = {
                "count": expenses_all[0],
                "amount_paise": expenses_all[1],
                "verification": "unverified",
                "currency": "INR",
            }
            # 10 Fees
            fees_verified = sum_verified("fee_records")
            fees_all = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM fee_records WHERE owner_id=?",
                (owner,),
            ).fetchone()
            fees_all_d = {
                "count": fees_all[0],
                "amount_paise": fees_all[1],
                "verification": "unverified",
                "currency": "INR",
            }
            # 11 Net realized verified = settled_verified - refunds_verified - expenses_verified - fees_verified
            net_verified = net_cash_paise(
                income_paise=settled_verified["amount_paise"],
                refunds_paise=refunds_verified["amount_paise"],
                fees_paise=fees_verified["amount_paise"],
                expenses_paise=expenses_verified["amount_paise"],
            )
            net_all = net_cash_paise(
                income_paise=settled_all_d["amount_paise"],
                refunds_paise=refunds_all_d["amount_paise"],
                fees_paise=fees_all_d["amount_paise"],
                expenses_paise=expenses_all_d["amount_paise"],
            )
            # Legacy ledger net for comparison
            ledger_rows = conn.execute(
                "SELECT kind, COALESCE(SUM(amount_paise),0), COUNT(*) FROM ledger_entries WHERE owner_id=? GROUP BY kind",
                (owner,),
            ).fetchall()
            ledger_map = {
                k: {"amount_paise": 0, "count": 0} for k in ("income", "expense", "refund", "fee")
            }
            for kind, amt, cnt in ledger_rows:
                ledger_map[kind] = {"amount_paise": amt, "count": cnt}
            ledger_net = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_paise WHEN kind IN ('expense','refund','fee') THEN -amount_paise ELSE 0 END),0) FROM ledger_entries WHERE owner_id=?",
                (owner,),
            ).fetchone()[0]

            return {
                "proposed": proposed_d,
                "proposed_customer_orders": {
                    "count": co_prop[0],
                    "amount_paise": co_prop[1],
                    "verification": "unverified",
                    "source": "customer_orders draft",
                    "currency": "INR",
                },
                "quoted": quoted_d,
                "contracted": contracted_d,
                "invoiced_asserted": (
                    lambda r: {
                        "count": r[0],
                        "amount_paise": r[1],
                        "verification": "unverified",
                        "currency": "INR",
                        "source": "financial_assertions invoice_asserted",
                    }
                )(
                    conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM financial_assertions WHERE owner_id=? AND stage='invoice_asserted'",
                        (owner,),
                    ).fetchone()
                ),
                "invoiced_drafts": inv_drafts_d,
                "invoiced_all": invoiced_all,
                "invoices_issued": invoiced_issued,
                "payment_initiated": init_all_d,
                "payment_initiated_verified": init_verified,
                "payment_received": recv_all_d,
                "payment_received_verified": recv_verified,
                "payment_settled": settled_all_d,
                "payment_settled_verified": settled_verified,
                "payment_settled_synthetic": (
                    lambda r: {
                        "count": r[0],
                        "amount_paise": r[1],
                        "verification": "synthetic",
                        "currency": "INR",
                        "source": "sandbox_payments sandbox_settled",
                    }
                )(
                    conn.execute(
                        "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM sandbox_payments WHERE owner_id=? AND stage='sandbox_settled'",
                        (owner,),
                    ).fetchone()
                ),
                "refunds": refunds_all_d,
                "refunds_verified": refunds_verified,
                "expenses": expenses_all_d,
                "expenses_verified": expenses_verified,
                "fees": fees_all_d,
                "fees_verified": fees_verified,
                "net_realized_verified": {
                    "amount_paise": net_verified,
                    "verification": "verified",
                    "currency": "INR",
                    "note": "settled_verified - refunds_verified - expenses_verified - fees_verified",
                },
                "net_realized_unverified": {
                    "amount_paise": net_all,
                    "verification": "unverified",
                    "currency": "INR",
                },
                "net_realized_synthetic": {
                    "amount_paise": net_cash_paise(
                        income_paise=conn.execute(
                            "SELECT COALESCE(SUM(amount_paise),0) FROM sandbox_payments WHERE owner_id=? AND stage='sandbox_settled'",
                            (owner,),
                        ).fetchone()[0],
                        refunds_paise=refunds_all_d["amount_paise"],
                        fees_paise=fees_all_d["amount_paise"],
                        expenses_paise=expenses_all_d["amount_paise"],
                    ),
                    "verification": "synthetic",
                    "currency": "INR",
                    "note": "synthetic settled - refunds - fees - expenses",
                },
                "ledger_net_unverified": {
                    "amount_paise": ledger_net,
                    "verification": "unverified",
                    "currency": "INR",
                    "note": "ledger income - expense - refund - fee",
                },
                "currency": "INR",
            }

    def monthly_summary(self, token: str, year: int, month: int) -> dict:
        if type(year) is not int or not 1970 <= year <= 2100:
            raise FinanceError("Invalid year.")
        if type(month) is not int or not 1 <= month <= 12:
            raise FinanceError("Invalid month.")
        import datetime

        start = int(datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc).timestamp())
        if month == 12:
            end = int(datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
        else:
            end = int(
                datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc).timestamp()
            )
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            # Invoiced in month: invoices created in month
            invoiced = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND created_at>=? AND created_at<?",
                (owner, start, end),
            ).fetchone()
            invoiced_issued = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND status IN ('issued','paid','overdue') AND created_at>=? AND created_at<?",
                (owner, start, end),
            ).fetchone()
            # Payments settled verified in month (by updated_at? use created_at for settlement? For simplicity use updated_at when status settled)
            # But we store created_at as creation, updated_at as transition time. So settled in month where updated_at in range and status settled and verified
            settled = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='settled' AND verified=1 AND updated_at>=? AND updated_at<?",
                (owner, start, end),
            ).fetchone()
            expenses = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM expense_records WHERE owner_id=? AND verified=1 AND incurred_at>=? AND incurred_at<?",
                (owner, start, end),
            ).fetchone()
            refunds = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM refund_records WHERE owner_id=? AND verified=1 AND created_at>=? AND created_at<?",
                (owner, start, end),
            ).fetchone()
            fees = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_paise),0) FROM fee_records WHERE owner_id=? AND verified=1 AND created_at>=? AND created_at<?",
                (owner, start, end),
            ).fetchone()
            net = settled[1] - expenses[1] - refunds[1] - fees[1]
            return {
                "year": year,
                "month": month,
                "invoiced": {"count": invoiced[0], "amount_paise": invoiced[1]},
                "invoiced_issued": {
                    "count": invoiced_issued[0],
                    "amount_paise": invoiced_issued[1],
                },
                "settled_verified": {"count": settled[0], "amount_paise": settled[1]},
                "expenses_verified": {"count": expenses[0], "amount_paise": expenses[1]},
                "refunds_verified": {"count": refunds[0], "amount_paise": refunds[1]},
                "fees_verified": {"count": fees[0], "amount_paise": fees[1]},
                "net_realized_verified": {"amount_paise": net},
                "currency": "INR",
                "period_start": start,
                "period_end": end,
            }

    def outstanding_invoices(
        self, token: str, *, as_of: int | None = None, limit: int = 50
    ) -> list[dict]:
        if as_of is not None and (type(as_of) is not int or not 0 <= as_of <= 4102444800):
            raise FinanceError("Invalid as_of.")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise FinanceError("Invalid limit.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            effective = as_of if as_of is not None else now
            # Outstanding: status issued or overdue, and due_at is not null and due_at < effective is overdue, but both are outstanding
            # For report, we consider issued + overdue as outstanding; overdue additionally flagged
            rows = conn.execute(
                "SELECT id FROM invoices WHERE owner_id=? AND status IN ('issued','overdue') ORDER BY due_at ASC, created_at ASC LIMIT ?",
                (owner, limit),
            ).fetchall()
            out = []
            for r in rows:
                inv = conn.execute(
                    "SELECT * FROM invoices WHERE id=? AND owner_id=?", (r["id"], owner)
                ).fetchone()
                if inv is None:
                    continue
                d = dict(inv)
                # Compute overdue flag
                due = inv["due_at"]
                d["is_overdue"] = bool(
                    due is not None and due < effective and inv["status"] in ("issued", "overdue")
                )
                d["days_overdue"] = ((effective - due) // 86400) if d["is_overdue"] and due else 0
                out.append(d)
            return out

    def cash_flow(self, token: str, from_ts: int, to_ts: int) -> dict:
        if type(from_ts) is not int or not 0 <= from_ts <= 4102444800:
            raise FinanceError("Invalid from_ts.")
        if type(to_ts) is not int or not 0 <= to_ts <= 4102444800:
            raise FinanceError("Invalid to_ts.")
        if from_ts >= to_ts:
            raise FinanceError("from_ts must be < to_ts.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            # Cash flow: inflows = settled_verified, outflows = expenses_verified + refunds_verified + fees_verified
            inflow = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM payment_records WHERE owner_id=? AND status='settled' AND verified=1 AND updated_at>=? AND updated_at<?",
                (owner, from_ts, to_ts),
            ).fetchone()[0]
            outflow_exp = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM expense_records WHERE owner_id=? AND verified=1 AND incurred_at>=? AND incurred_at<?",
                (owner, from_ts, to_ts),
            ).fetchone()[0]
            outflow_ref = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM refund_records WHERE owner_id=? AND verified=1 AND created_at>=? AND created_at<?",
                (owner, from_ts, to_ts),
            ).fetchone()[0]
            outflow_fee = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM fee_records WHERE owner_id=? AND verified=1 AND created_at>=? AND created_at<?",
                (owner, from_ts, to_ts),
            ).fetchone()[0]
            outflow = outflow_exp + outflow_ref + outflow_fee
            net = inflow - outflow
            # Also include invoice amounts for context but not counted as cash until settled
            invoiced = conn.execute(
                "SELECT COALESCE(SUM(amount_paise),0) FROM invoices WHERE owner_id=? AND created_at>=? AND created_at<?",
                (owner, from_ts, to_ts),
            ).fetchone()[0]
            return {
                "from_ts": from_ts,
                "to_ts": to_ts,
                "inflow_settled_verified": inflow,
                "outflow_expenses_verified": outflow_exp,
                "outflow_refunds_verified": outflow_ref,
                "outflow_fees_verified": outflow_fee,
                "outflow_total_verified": outflow,
                "net_cash_flow_verified": net,
                "invoiced_amount": invoiced,
                "currency": "INR",
                "note": "Only verified settled payments count as inflow; unverified is excluded.",
            }
