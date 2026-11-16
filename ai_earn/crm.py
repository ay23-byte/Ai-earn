"""Proper CRM — Phase 8 (v27).

Owner-scoped, session-authorized, transactional, idempotent via operation_key,
audited via state_audit.

Covers:
- Customer profiles (customers table v17)
- Contact information (customers.contact)
- Customer notes (customer_notes v27)
- Lead source (customers.source)
- Communication history (communications v18) with approval gate
- Customer status (lead/contacted/converted/rejected)
- Follow-up reminders (followups v19)
- Customer requirements (customer_requirements v27)
- Proposals (proposals v25 filtered by customer_id)
- Orders (customer_orders v27)
- Project history (customer_projects v27 linking customers ↔ project_tasks)
- Customer activity timeline (aggregated cross-table)

Account isolation: every query WHERE owner_id=?.
CSV import/export with validation via Customers.
Outbound guard: Communications.sent via CRM requires explicit approval
(CommunicationApprovals pending→approved, 15 min TTL, virtual expiry) unless
owner has explicitly configured a safe limited workflow
(OutboundWorkflowConfig enabled + channel allowlist + daily_limit).

Never send unsolicited messages automatically — no background auto-send;
every send() checks approval or safe workflow.
"""
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

# Resource types for state_operations / state_audit
_RESOURCE_NOTE = "customer_note"
_RESOURCE_REQUIREMENT = "customer_requirement"
_RESOURCE_ORDER = "customer_order"
_RESOURCE_PROJECT_LINK = "customer_project"
_RESOURCE_COMM_APPROVAL = "crm_communication_approval"
_RESOURCE_OUTBOUND_CONFIG = "crm_outbound_config"

# Order FSM
_ORDER_STATUSES = frozenset({"draft", "confirmed", "fulfilled", "cancelled"})
_ORDER_TRANSITIONS = {
    (None, "customer_order_created"): "draft",
    ("draft", "customer_order_confirmed"): "confirmed",
    ("draft", "customer_order_cancelled"): "cancelled",
    ("confirmed", "customer_order_fulfilled"): "fulfilled",
    ("confirmed", "customer_order_cancelled"): "cancelled",
}

# Approval FSM (similar to ai_approvals but CRM-specific)
_APPROVAL_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})

class CRMError(Exception):
    pass

def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, CRMError) as exc:
        raise CRMError(str(exc)) from exc
    except Exception as exc:
        raise CRMError("Invalid CRM input.") from exc

def _note(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2000 or len(value.strip()) == 0:
        raise CRMError("Invalid note.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise CRMError("Invalid note.")
    return value

def _requirement(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 500 or len(value.strip()) == 0:
        raise CRMError("Invalid requirement.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CRMError("Invalid requirement.")
    return value

def _title(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or len(value.strip()) == 0:
        raise CRMError("Invalid title.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CRMError("Invalid title.")
    return value

def _description(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise CRMError("Invalid description.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise CRMError("Invalid description.")
    return value

def _amount(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
        raise CRMError("Invalid amount.")
    return value

def _channel(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 30:
        raise CRMError("Invalid channel.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CRMError("Invalid channel.")
    return value

def _body(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise CRMError("Invalid body.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise CRMError("Invalid body.")
    return value

def _allowed_channels(value: str) -> str:
    if not isinstance(value, str) or len(value) > 500:
        raise CRMError("Invalid allowed_channels.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CRMError("Invalid allowed_channels.")
    # validate comma separated tokens each 1..30 without control
    if value.strip() == "":
        return ""
    parts = [p.strip() for p in value.split(",")]
    for p in parts:
        if not 1 <= len(p) <= 30 or any(ord(c) < 32 or ord(c) == 127 for c in p):
            raise CRMError("Invalid allowed_channels entry.")
        try:
            p.encode("utf-8")
        except Exception:
            raise CRMError("Invalid allowed_channels entry.")
    return value

def _daily_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise CRMError("Invalid daily_limit.")
    return value

def _ttl(value: int) -> int:
    if type(value) is not int or not 60 <= value <= 86400:
        raise CRMError("Invalid ttl.")
    return value

def _version_check(conn: sqlite3.Connection, min_version: int = 27) -> None:
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    from .state_storage import SUPPORTED_STATE_VERSIONS
    if ver not in SUPPORTED_STATE_VERSIONS or ver < min_version:
        raise StateStorageError(f"State schema version {min_version} is required for CRM.")

def _retry(conn, owner, key, digest, resource, table, id_col="id"):
    row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
    if row is None:
        return None
    if row["resource_type"] != resource or not hmac.compare_digest(row["request_hash"], digest):
        raise CRMError("Operation key was already used for a different request.")
    # For outbound config, resource_id is owner_id itself
    check_id = row["resource_id"]
    if table == "crm_outbound_configs":
        if conn.execute("SELECT 1 FROM crm_outbound_configs WHERE owner_id=?", (owner,)).fetchone() is None:
            raise StateStorageError("Stored operation result is unavailable.")
    else:
        if conn.execute(f"SELECT 1 FROM {table} WHERE {id_col}=? AND owner_id=?", (check_id, owner)).fetchone() is None:
            raise StateStorageError("Stored operation result is unavailable.")
    return check_id

def _audit(conn, now, owner, event, resource, rid):
    conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (now, owner, event, resource, rid))

def _remember(conn, owner, key, digest, resource, rid):
    conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, key, digest, resource, rid))

def _request(action: str, values: list) -> bytes:
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))

def _owned_customer(conn, owner, cid):
    row = conn.execute("SELECT * FROM customers WHERE id=? AND owner_id=?", (cid, owner)).fetchone()
    if row is None:
        raise CRMError("Customer not found.")
    return row

def _owned_communication(conn, owner, comm_id):
    row = conn.execute("SELECT * FROM communications WHERE id=? AND owner_id=?", (comm_id, owner)).fetchone()
    if row is None:
        raise CRMError("Communication not found.")
    return row

def _owned_project_task(conn, owner, task_id):
    row = conn.execute("SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (task_id, owner)).fetchone()
    if row is None:
        raise CRMError("Project task not found.")
    return row

# ---------- Customer Notes ----------
class CustomerNotes:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state

    def add(self, token: str, customer_id: str, note: str, *, operation_key: str) -> str:
        cid = _input(_record_id, customer_id)
        note_v = _input(_note, note)
        key = _input(_key, operation_key)
        digest = _request("customer_note_create", [cid, note_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_NOTE, "customer_notes")
            if existing:
                return existing
            _owned_customer(conn, owner, cid)
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO customer_notes(id, owner_id, customer_id, note, created_at) VALUES(?,?,?,?,?)", (ident, owner, cid, note_v, now))
            _audit(conn, now, owner, "customer_note_created", _RESOURCE_NOTE, ident)
            _remember(conn, owner, key, digest, _RESOURCE_NOTE, ident)
            return ident

    def list(self, token: str, customer_id: str) -> list[dict]:
        cid = _input(_record_id, customer_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_customer(conn, owner, cid)
            rows = conn.execute("SELECT * FROM customer_notes WHERE owner_id=? AND customer_id=? ORDER BY created_at, id", (owner, cid)).fetchall()
            return [dict(r) for r in rows]

    def get(self, token: str, note_id: str) -> dict:
        nid = _input(_record_id, note_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = conn.execute("SELECT * FROM customer_notes WHERE id=? AND owner_id=?", (nid, owner)).fetchone()
            if row is None:
                raise AccessDenied("Access denied.")
            return dict(row)

# ---------- Customer Requirements ----------
class CustomerRequirements:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state

    def add(self, token: str, customer_id: str, requirement: str, *, operation_key: str) -> str:
        cid = _input(_record_id, customer_id)
        req_v = _input(_requirement, requirement)
        key = _input(_key, operation_key)
        digest = _request("customer_requirement_create", [cid, req_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_REQUIREMENT, "customer_requirements")
            if existing:
                return existing
            _owned_customer(conn, owner, cid)
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO customer_requirements(id, owner_id, customer_id, requirement, created_at) VALUES(?,?,?,?,?)", (ident, owner, cid, req_v, now))
            _audit(conn, now, owner, "customer_requirement_created", _RESOURCE_REQUIREMENT, ident)
            _remember(conn, owner, key, digest, _RESOURCE_REQUIREMENT, ident)
            return ident

    def list(self, token: str, customer_id: str) -> list[dict]:
        cid = _input(_record_id, customer_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_customer(conn, owner, cid)
            rows = conn.execute("SELECT * FROM customer_requirements WHERE owner_id=? AND customer_id=? ORDER BY created_at, id", (owner, cid)).fetchall()
            return [dict(r) for r in rows]

    def remove(self, token: str, requirement_id: str, *, operation_key: str) -> str:
        rid = _input(_record_id, requirement_id)
        key = _input(_key, operation_key)
        digest = _request("customer_requirement_remove", [rid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_REQUIREMENT or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise CRMError("Operation key was already used for a different request.")
                return rid
            if conn.execute("SELECT 1 FROM customer_requirements WHERE id=? AND owner_id=?", (rid, owner)).fetchone() is None:
                raise AccessDenied("Access denied.")
            conn.execute("DELETE FROM customer_requirements WHERE id=? AND owner_id=?", (rid, owner))
            _audit(conn, now, owner, "customer_requirement_deleted", _RESOURCE_REQUIREMENT, rid)
            _remember(conn, owner, key, digest, _RESOURCE_REQUIREMENT, rid)
            return rid

# ---------- Customer Orders ----------
class CustomerOrders:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state

    def _owned(self, conn, owner, oid):
        row = conn.execute("SELECT * FROM customer_orders WHERE id=? AND owner_id=?", (oid, owner)).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        # replay audit for consistency
        events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id", (owner, _RESOURCE_ORDER, oid)).fetchall()
        if not events:
            raise StateStorageError("Customer order history inconsistent.")
        replayed = None
        for (ev,) in events:
            if (replayed, ev) not in _ORDER_TRANSITIONS:
                raise StateStorageError("Customer order history inconsistent.")
            replayed = _ORDER_TRANSITIONS[(replayed, ev)]
        if replayed != row["status"]:
            raise StateStorageError("Customer order history inconsistent.")
        cnt = conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?", (owner, _RESOURCE_ORDER, oid)).fetchone()[0]
        if cnt != len(events):
            raise StateStorageError("Customer order history inconsistent.")
        return row

    def create(self, token: str, customer_id: str, title: str, description: str = "", amount_paise: int = 0, *, operation_key: str) -> str:
        cid = _input(_record_id, customer_id)
        title_v = _input(_title, title)
        desc_v = _input(_description, description)
        amount_v = _input(_amount, amount_paise)
        key = _input(_key, operation_key)
        digest = _request("customer_order_create", [cid, title_v, desc_v, amount_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_ORDER, "customer_orders")
            if existing:
                return existing
            _owned_customer(conn, owner, cid)
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO customer_orders(id, owner_id, customer_id, title, description, amount_paise, currency, status, created_at, updated_at) VALUES(?,?,?,?,?,?, 'INR', 'draft', ?, ?)", (ident, owner, cid, title_v, desc_v, amount_v, now, now))
            _audit(conn, now, owner, "customer_order_created", _RESOURCE_ORDER, ident)
            _remember(conn, owner, key, digest, _RESOURCE_ORDER, ident)
            return ident

    def get(self, token: str, order_id: str) -> dict:
        oid = _input(_record_id, order_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(self._owned(conn, owner, oid))

    def list(self, token: str, customer_id: str | None = None, *, status: str | None = None, after_id: str | None = None, limit: int = 20) -> list[dict]:
        if customer_id is not None:
            cid = _input(_record_id, customer_id)
        else:
            cid = None
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CRMError("Invalid limit.")
        if status is not None and status not in _ORDER_STATUSES:
            raise CRMError("Invalid status.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            if cid is not None:
                _owned_customer(conn, owner, cid)
            query = "SELECT id FROM customer_orders WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if cid is not None:
                query += " AND customer_id=?"
                params.append(cid)
            if status is not None:
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            out = []
            for r in rows:
                out.append(dict(self._owned(conn, owner, r["id"])))
            return out

    def _transition(self, token, order_id, target, event, operation_key):
        oid = _input(_record_id, order_id)
        key = _input(_key, operation_key)
        if target not in _ORDER_STATUSES:
            raise CRMError("Invalid status.")
        digest = _request("customer_order_transition", [oid, target])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = self._owned(conn, owner, oid)
            existing = _retry(conn, owner, key, digest, _RESOURCE_ORDER, "customer_orders")
            if existing:
                return oid
            cur = row["status"]
            if _ORDER_TRANSITIONS.get((cur, event)) != target:
                raise CRMError("Transition not allowed.")
            conn.execute("UPDATE customer_orders SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, oid, owner))
            _audit(conn, now, owner, event, _RESOURCE_ORDER, oid)
            _remember(conn, owner, key, digest, _RESOURCE_ORDER, oid)
            return oid

    def confirm(self, token: str, order_id: str, *, operation_key: str) -> str:
        return self._transition(token, order_id, "confirmed", "customer_order_confirmed", operation_key)

    def fulfill(self, token: str, order_id: str, *, operation_key: str) -> str:
        return self._transition(token, order_id, "fulfilled", "customer_order_fulfilled", operation_key)

    def cancel(self, token: str, order_id: str, *, operation_key: str) -> str:
        oid = _input(_record_id, order_id)
        key = _input(_key, operation_key)
        digest = _request("customer_order_transition", [oid, "cancelled"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = self._owned(conn, owner, oid)
            existing = _retry(conn, owner, key, digest, _RESOURCE_ORDER, "customer_orders")
            if existing:
                return oid
            cur = row["status"]
            if _ORDER_TRANSITIONS.get((cur, "customer_order_cancelled")) != "cancelled":
                raise CRMError("Transition not allowed.")
            conn.execute("UPDATE customer_orders SET status='cancelled', updated_at=? WHERE id=? AND owner_id=?", (now, oid, owner))
            _audit(conn, now, owner, "customer_order_cancelled", _RESOURCE_ORDER, oid)
            _remember(conn, owner, key, digest, _RESOURCE_ORDER, oid)
            return oid

# ---------- Customer Projects (history) ----------
class CustomerProjects:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state

    def link(self, token: str, customer_id: str, project_task_id: str, *, operation_key: str) -> str:
        cid = _input(_record_id, customer_id)
        pid = _input(_record_id, project_task_id)
        key = _input(_key, operation_key)
        digest = _request("customer_project_link", [cid, pid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_PROJECT_LINK, "customer_projects")
            if existing:
                return existing
            _owned_customer(conn, owner, cid)
            _owned_project_task(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO customer_projects(id, owner_id, customer_id, project_task_id, created_at) VALUES(?,?,?,?,?)", (ident, owner, cid, pid, now))
            _audit(conn, now, owner, "customer_project_linked", _RESOURCE_PROJECT_LINK, ident)
            _remember(conn, owner, key, digest, _RESOURCE_PROJECT_LINK, ident)
            return ident

    def list(self, token: str, customer_id: str) -> list[dict]:
        cid = _input(_record_id, customer_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_customer(conn, owner, cid)
            rows = conn.execute("SELECT * FROM customer_projects WHERE owner_id=? AND customer_id=? ORDER BY created_at, id", (owner, cid)).fetchall()
            out = []
            for r in rows:
                # enrich with project_task details
                task = conn.execute("SELECT * FROM project_tasks WHERE id=? AND owner_id=?", (r["project_task_id"], owner)).fetchone()
                d = dict(r)
                d["project_task"] = dict(task) if task else None
                out.append(d)
            return out

    def unlink(self, token: str, link_id: str, *, operation_key: str) -> str:
        lid = _input(_record_id, link_id)
        key = _input(_key, operation_key)
        digest = _request("customer_project_unlink", [lid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_PROJECT_LINK or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise CRMError("Operation key was already used for a different request.")
                return lid
            if conn.execute("SELECT 1 FROM customer_projects WHERE id=? AND owner_id=?", (lid, owner)).fetchone() is None:
                raise AccessDenied("Access denied.")
            conn.execute("DELETE FROM customer_projects WHERE id=? AND owner_id=?", (lid, owner))
            _audit(conn, now, owner, "customer_project_unlinked", _RESOURCE_PROJECT_LINK, lid)
            _remember(conn, owner, key, digest, _RESOURCE_PROJECT_LINK, lid)
            return lid

# ---------- Communication Approvals ----------
class CommunicationApprovals:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state

    def _effective_status(self, row, now):
        # row is sqlite Row or dict-like with status, created_at, expires_at
        status = row["status"]
        if status == "pending" and now > row["expires_at"]:
            return "expired"
        return status

    def request(self, token: str, communication_id: str, *, operation_key: str, ttl: int = 900) -> str:
        cid = _input(_record_id, communication_id)
        ttl_v = _input(_ttl, ttl)
        key = _input(_key, operation_key)
        digest = _request("crm_comm_approval_request", [cid, ttl_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_COMM_APPROVAL, "crm_communication_approvals")
            if existing:
                return existing
            _owned_communication(conn, owner, cid)
            # ensure communication is draft?
            # optional: check status draft
            comm = conn.execute("SELECT status FROM communications WHERE id=? AND owner_id=?", (cid, owner)).fetchone()
            if comm and comm["status"] not in ("draft", "sent", "archived"):
                raise CRMError("Invalid communication status for approval.")
            ident = secrets.token_hex(16)
            expires = now + ttl_v
            conn.execute("INSERT INTO crm_communication_approvals(id, owner_id, communication_id, status, created_at, expires_at, decided_at) VALUES(?,?,?,?,?,?, NULL)", (ident, owner, cid, "pending", now, expires))
            _audit(conn, now, owner, "crm_communication_approval_created", _RESOURCE_COMM_APPROVAL, ident)
            _remember(conn, owner, key, digest, _RESOURCE_COMM_APPROVAL, ident)
            return ident

    def _transition(self, token, approval_id, target, event, operation_key):
        aid = _input(_record_id, approval_id)
        key = _input(_key, operation_key)
        if target not in ("approved", "rejected"):
            raise CRMError("Invalid target.")
        digest = _request("crm_comm_approval_decide", [aid, target])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute("SELECT * FROM crm_communication_approvals WHERE id=? AND owner_id=?", (aid, owner)).fetchone()
            if row is None:
                raise AccessDenied("Access denied.")
            existing = _retry(conn, owner, key, digest, _RESOURCE_COMM_APPROVAL, "crm_communication_approvals")
            if existing:
                return aid
            eff = self._effective_status(row, now)
            if eff != "pending":
                raise CRMError(f"Approval is {eff}, cannot decide.")
            conn.execute("UPDATE crm_communication_approvals SET status=?, decided_at=? WHERE id=? AND owner_id=?", (target, now, aid, owner))
            _audit(conn, now, owner, event, _RESOURCE_COMM_APPROVAL, aid)
            _remember(conn, owner, key, digest, _RESOURCE_COMM_APPROVAL, aid)
            return aid

    def approve(self, token: str, approval_id: str, *, operation_key: str) -> str:
        return self._transition(token, approval_id, "approved", "crm_communication_approval_approved", operation_key)

    def reject(self, token: str, approval_id: str, *, operation_key: str) -> str:
        return self._transition(token, approval_id, "rejected", "crm_communication_approval_rejected", operation_key)

    def get(self, token: str, approval_id: str) -> dict:
        aid = _input(_record_id, approval_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute("SELECT * FROM crm_communication_approvals WHERE id=? AND owner_id=?", (aid, owner)).fetchone()
            if row is None:
                raise AccessDenied("Access denied.")
            d = dict(row)
            d["effective_status"] = self._effective_status(row, now)
            return d

    def list(self, token: str, *, communication_id: str | None = None, after_id: str | None = None, limit: int = 20) -> list[dict]:
        cid = _input(_record_id, communication_id) if communication_id else None
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CRMError("Invalid limit.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            query = "SELECT * FROM crm_communication_approvals WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if cid:
                query += " AND communication_id=?"
                params.append(cid)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["effective_status"] = self._effective_status(r, now)
                out.append(d)
            return out

# ---------- Outbound Workflow Config (safe limited workflow) ----------
class OutboundWorkflowConfig:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state

    def set(self, token: str, *, operation_key: str, enabled: bool, allowed_channels: str = "", daily_limit: int = 5) -> str:
        if type(enabled) is not bool:
            raise CRMError("enabled bool required.")
        allowed_v = _input(_allowed_channels, allowed_channels)
        limit_v = _input(_daily_limit, daily_limit)
        key = _input(_key, operation_key)
        digest = _request("crm_outbound_config_set", [1 if enabled else 0, allowed_v, limit_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            # check if existing operation key with same digest
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_OUTBOUND_CONFIG or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise CRMError("Operation key was already used for a different request.")
                return owner
            # need to check previous config hash? We just upsert
            exists = conn.execute("SELECT 1 FROM crm_outbound_configs WHERE owner_id=?", (owner,)).fetchone()
            if exists:
                conn.execute("UPDATE crm_outbound_configs SET enabled=?, allowed_channels=?, daily_limit=?, updated_at=? WHERE owner_id=?", (1 if enabled else 0, allowed_v, limit_v, now, owner))
            else:
                conn.execute("INSERT INTO crm_outbound_configs(owner_id, enabled, allowed_channels, daily_limit, updated_at) VALUES(?,?,?,?,?)", (owner, 1 if enabled else 0, allowed_v, limit_v, now))
            _audit(conn, now, owner, "crm_outbound_config_set", _RESOURCE_OUTBOUND_CONFIG, owner)
            _remember(conn, owner, key, digest, _RESOURCE_OUTBOUND_CONFIG, owner)
            return owner

    def get(self, token: str) -> dict | None:
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = conn.execute("SELECT * FROM crm_outbound_configs WHERE owner_id=?", (owner,)).fetchone()
            if row is None:
                return None
            return dict(row)

    def clear(self, token: str, *, operation_key: str) -> str:
        key = _input(_key, operation_key)
        digest = _request("crm_outbound_config_clear", [])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_OUTBOUND_CONFIG or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise CRMError("Operation key was already used for a different request.")
                return owner
            conn.execute("DELETE FROM crm_outbound_configs WHERE owner_id=?", (owner,))
            _audit(conn, now, owner, "crm_outbound_config_cleared", _RESOURCE_OUTBOUND_CONFIG, owner)
            _remember(conn, owner, key, digest, _RESOURCE_OUTBOUND_CONFIG, owner)
            return owner

    def is_safe_workflow(self, conn, owner, channel: str) -> bool:
        # call inside transaction where outbound config already fetched
        row = conn.execute("SELECT * FROM crm_outbound_configs WHERE owner_id=?", (owner,)).fetchone()
        if row is None or row["enabled"] != 1:
            return False
        allowed = row["allowed_channels"]
        if allowed.strip() == "":
            return False
        allowed_list = [p.strip() for p in allowed.split(",") if p.strip()]
        if channel not in allowed_list:
            return False
        # daily_limit enforcement: count sent communications today per owner for this channel
        # Use calendar day UTC: today = now // 86400 * 86400
        # We need now timestamp; caller provides now via conn? We'll compute via current time inside check
        # For simplicity, count all sent since midnight UTC
        # To avoid needing now, we can count last 24h? Use 86400 window.
        # Let's require caller to pass now.
        return True  # channel check only; daily count checked separately

# ---------- CRM Communications Gate ----------
class CRMCommunications:
    """Wraps Communications with approval gate and safe workflow."""
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state
        self._approvals = CommunicationApprovals(auth, state)
        self._outbound = OutboundWorkflowConfig(auth, state)

    def create_draft(self, token: str, customer_id: str, channel: str, body: str, *, operation_key: str) -> str:
        # delegate to Communications.create
        from .communications import Communications
        comm = Communications(self._auth, self._state)
        return comm.create(token, customer_id, channel, body, operation_key=operation_key)

    def request_approval(self, token: str, communication_id: str, *, operation_key: str, ttl: int = 900) -> str:
        return self._approvals.request(token, communication_id, operation_key=operation_key, ttl=ttl)

    def _check_safe_workflow(self, conn, owner, channel, now) -> bool:
        row = conn.execute("SELECT * FROM crm_outbound_configs WHERE owner_id=?", (owner,)).fetchone()
        if row is None or row["enabled"] != 1:
            return False
        allowed = row["allowed_channels"]
        if not allowed.strip():
            return False
        allowed_list = [p.strip() for p in allowed.split(",") if p.strip()]
        if channel not in allowed_list:
            return False
        # daily limit: count communications sent today (UTC day) for this owner/channel
        # Determine day start
        day_start = (now // 86400) * 86400
        cnt = conn.execute("SELECT count(*) FROM communications WHERE owner_id=? AND channel=? AND status='sent' AND updated_at >= ?", (owner, channel, day_start)).fetchone()[0]
        if cnt >= row["daily_limit"]:
            return False
        return True

    def _derived_comm_key(self, key: str) -> str:
        # Derive a valid _key for the internal communication transition
        if len(key) <= 122:
            candidate = key + "_comm"
            # ensure matches [A-Za-z0-9_-]
            if candidate.replace("_", "").replace("-", "").isalnum():
                return candidate
            # fallback to hash if chars invalid (should not happen as _key already validated)
            return candidate[:128]
        # too long, hash to 32 hex
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def send(self, token: str, communication_id: str, *, operation_key: str, approval_id: str | None = None) -> str:
        cid = _input(_record_id, communication_id)
        key = _input(_key, operation_key)
        if approval_id is not None:
            approval_id = _input(_record_id, approval_id)
        digest_vals = [cid, approval_id or ""]
        digest = _request("crm_comm_send", digest_vals)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != "crm_communication_send" or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise CRMError("Operation key was already used for a different request.")
                return cid
            comm = _owned_communication(conn, owner, cid)
            if comm["status"] != "draft":
                raise CRMError("Only draft can be sent.")
            safe = self._check_safe_workflow(conn, owner, comm["channel"], now)
            if not safe:
                if approval_id is None:
                    raise CRMError("Outbound communication requires user approval (or explicitly configured safe limited workflow).")
                appr = conn.execute("SELECT * FROM crm_communication_approvals WHERE id=? AND owner_id=?", (approval_id, owner)).fetchone()
                if appr is None:
                    raise CRMError("Approval not found.")
                if appr["communication_id"] != cid:
                    raise CRMError("Approval does not match communication.")
                eff = appr["status"]
                if eff == "pending" and now > appr["expires_at"]:
                    eff = "expired"
                if eff != "approved":
                    raise CRMError(f"Approval is {eff}, cannot send.")
            from .communications import _TRANSITIONS as COMM_TRANSITIONS
            events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type='communication' AND resource_id=? ORDER BY id", (owner, cid)).fetchall()
            replayed = None
            for (ev,) in events:
                if (replayed, ev) not in COMM_TRANSITIONS:
                    raise StateStorageError("Communication history inconsistent.")
                replayed = COMM_TRANSITIONS[(replayed, ev)]
            if replayed != comm["status"]:
                raise StateStorageError("Communication history inconsistent.")
            expected = COMM_TRANSITIONS.get((replayed, "communication_sent"))
            if expected != "sent":
                raise CRMError("Transition not allowed.")
            # Update communications status
            conn.execute("UPDATE communications SET status='sent', updated_at=? WHERE id=? AND owner_id=?", (now, cid, owner))
            _audit(conn, now, owner, "communication_sent", "communication", cid)
            # Also need to record the communication's state_operations for the transition
            # Derive a comm operation key and remember it so that _owned count matches events
            derived_key = self._derived_comm_key(key)
            # Avoid collision if derived already exists (idempotency)
            existing_comm_op = conn.execute("SELECT 1 FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, derived_key)).fetchone()
            if existing_comm_op is None:
                # compute digest for communication transition as Communications does
                comm_digest = _base_digest("communication_transition", json.dumps([cid, "sent"], ensure_ascii=True, separators=(",", ":")))
                # Check if derived key was used for different request? Not needed as fresh
                conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, derived_key, comm_digest, "communication", cid))
            # Remember CRM operation
            _remember(conn, owner, key, digest, "crm_communication_send", cid)
            return cid

    def list_history(self, token: str, customer_id: str, *, status: str | None = None, after_id: str | None = None, limit: int = 20) -> list[dict]:
        from .communications import Communications
        comm = Communications(self._auth, self._state)
        return comm.list(token, after_id=after_id, limit=limit, customer_id=customer_id, status=status)

# ---------- Activity Timeline ----------
class ActivityTimeline:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state

    def get(self, token: str, customer_id: str, *, limit: int = 100) -> list[dict]:
        cid = _input(_record_id, customer_id)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise CRMError("Invalid limit.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_customer(conn, owner, cid)
            events: list[dict] = []
            # customer creation/status via state_audit for resource_type customer
            rows = conn.execute("SELECT event, occurred_at, resource_id FROM state_audit WHERE actor_id=? AND resource_type='customer' AND resource_id=? ORDER BY occurred_at", (owner, cid)).fetchall()
            for r in rows:
                events.append({"type": "customer", "event": r["event"], "at": r["occurred_at"], "id": r["resource_id"], "summary": r["event"]})
            # notes
            for r in conn.execute("SELECT id, note, created_at FROM customer_notes WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchall():
                events.append({"type": "note", "event": "customer_note_created", "at": r["created_at"], "id": r["id"], "summary": r["note"][:60]})
            # requirements
            for r in conn.execute("SELECT id, requirement, created_at FROM customer_requirements WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchall():
                events.append({"type": "requirement", "event": "customer_requirement_created", "at": r["created_at"], "id": r["id"], "summary": r["requirement"][:60]})
            # communications
            for r in conn.execute("SELECT id, channel, body, status, created_at, updated_at FROM communications WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchall():
                # creation
                events.append({"type": "communication", "event": f"communication_{r['status']}", "at": r["created_at"], "id": r["id"], "summary": f"{r['channel']}: {r['body'][:40]}"})
                if r["status"] == "sent":
                    events.append({"type": "communication", "event": "communication_sent", "at": r["updated_at"], "id": r["id"], "summary": f"sent {r['channel']}"})
            # followups
            for r in conn.execute("SELECT id, due_at, note, status, created_at FROM followups WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchall():
                events.append({"type": "followup", "event": f"followup_{r['status']}", "at": r["created_at"], "id": r["id"], "summary": r["note"][:40]})
                # status via audit? also add updated maybe
            # proposals filtered by customer_id
            for r in conn.execute("SELECT id, title, status, created_at, updated_at FROM proposals WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchall():
                events.append({"type": "proposal", "event": f"proposal_{r['status']}", "at": r["created_at"], "id": r["id"], "summary": r["title"][:40]})
            # customer orders
            for r in conn.execute("SELECT id, title, status, created_at FROM customer_orders WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchall():
                events.append({"type": "order", "event": f"order_{r['status']}", "at": r["created_at"], "id": r["id"], "summary": r["title"][:40]})
            # customer projects
            for r in conn.execute("SELECT id, project_task_id, created_at FROM customer_projects WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchall():
                task = conn.execute("SELECT title, status FROM project_tasks WHERE id=? AND owner_id=?", (r["project_task_id"], owner)).fetchone()
                title = task["title"][:40] if task else r["project_task_id"][:8]
                events.append({"type": "project", "event": "project_linked", "at": r["created_at"], "id": r["id"], "summary": title})
                if task:
                    events.append({"type": "project", "event": f"project_{task['status']}", "at": task["updated_at"] if "updated_at" in task.keys() else r["created_at"], "id": r["project_task_id"], "summary": title})
            # Sort by at ascending, then limit
            events.sort(key=lambda x: x["at"])
            # dedup maybe sort
            return events[:limit]

# ---------- Customer Profile (aggregated view) ----------
class CustomerProfile:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state
        self._notes = CustomerNotes(auth, state)
        self._reqs = CustomerRequirements(auth, state)
        self._orders = CustomerOrders(auth, state)
        self._projects = CustomerProjects(auth, state)
        self._timeline = ActivityTimeline(auth, state)

    def get(self, token: str, customer_id: str) -> dict:
        cid = _input(_record_id, customer_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            cust = dict(_owned_customer(conn, owner, cid))
            # enrich counts
            cust["_note_count"] = conn.execute("SELECT count(*) FROM customer_notes WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchone()[0]
            cust["_requirement_count"] = conn.execute("SELECT count(*) FROM customer_requirements WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchone()[0]
            cust["_communication_count"] = conn.execute("SELECT count(*) FROM communications WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchone()[0]
            cust["_followup_count"] = conn.execute("SELECT count(*) FROM followups WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchone()[0]
            cust["_order_count"] = conn.execute("SELECT count(*) FROM customer_orders WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchone()[0]
            cust["_project_count"] = conn.execute("SELECT count(*) FROM customer_projects WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchone()[0]
            cust["_proposal_count"] = conn.execute("SELECT count(*) FROM proposals WHERE owner_id=? AND customer_id=?", (owner, cid)).fetchone()[0]
            return cust

    def full(self, token: str, customer_id: str) -> dict:
        # returns aggregated with timeline
        profile = self.get(token, customer_id)
        notes = self._notes.list(token, customer_id)
        reqs = self._reqs.list(token, customer_id)
        from .communications import Communications
        from .followups import Followups
        comms = Communications(self._auth, self._state).list(token, customer_id=customer_id, limit=100)
        folls = Followups(self._auth, self._state).list(token, customer_id=customer_id, limit=100)
        orders = self._orders.list(token, customer_id=customer_id, limit=100)
        projects = self._projects.list(token, customer_id)
        from .service_workspace import Proposals
        proposals = Proposals(self._auth, self._state).list(token, customer_id=customer_id, limit=100)
        timeline = self._timeline.get(token, customer_id, limit=100)
        return {
            "profile": profile,
            "notes": notes,
            "requirements": reqs,
            "communications": comms,
            "followups": folls,
            "orders": orders,
            "projects": projects,
            "proposals": proposals,
            "timeline": timeline,
        }

# ---------- CSV helpers (wrappers) ----------
class CustomerCSV:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth; self._state = state

    def export(self, token: str) -> str:
        from .customers import Customers
        return Customers(self._auth, self._state).export_csv(token)

    def import_csv(self, token: str, csv_text: str, *, operation_key_prefix: str) -> dict:
        from .customers import Customers
        return Customers(self._auth, self._state).import_csv(token, csv_text, operation_key_prefix=operation_key_prefix)

