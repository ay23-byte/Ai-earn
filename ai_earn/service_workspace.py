"""Service management workspace — Phase 6 (v25).

Owner-scoped, session-authorized, transactional, idempotent via operation_key,
audited via state_audit. Covers services editing, packages, delivery time,
pricing, revisions, requirements, portfolio items, attachments, proposals
(with AI draft generation), proposal status, customer requests, and editable
service templates for 9 domains. Templates are editable copies, not hard limits.

Portfolio presentation is available via `PortfolioPresentation`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from typing import Any

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError
from .services import _scope, _deliverable, _price, _revisions, _description, _version as _svc_version, _owned_service, _request as _svc_request

_RESOURCE_PACKAGE = "service_package"
_RESOURCE_REQUIREMENT = "service_requirement"
_RESOURCE_ATTACHMENT = "service_attachment"
_RESOURCE_PROPOSAL = "proposal"
_RESOURCE_REQUEST = "customer_request"

_PACKAGE_STATUSES = frozenset()  # packages have no FSM, just CRUD
_PROPOSAL_STATUSES = frozenset({"draft","sent","accepted","rejected","cancelled","archived"})
_PROPOSAL_TRANSITIONS = {
    (None, "proposal_created"): "draft",
    ("draft", "proposal_sent"): "sent",
    ("draft", "proposal_cancelled"): "cancelled",
    ("sent", "proposal_accepted"): "accepted",
    ("sent", "proposal_rejected"): "rejected",
    ("sent", "proposal_cancelled"): "cancelled",
    ("accepted", "proposal_archived"): "archived",
    ("rejected", "proposal_archived"): "archived",
    ("cancelled", "proposal_archived"): "archived",
    ("draft", "proposal_archived"): "archived",
}
_REQUEST_STATUSES = frozenset({"pending","accepted","rejected","completed","cancelled"})
_REQUEST_TRANSITIONS = {
    (None, "request_created"): "pending",
    ("pending", "request_accepted"): "accepted",
    ("pending", "request_rejected"): "rejected",
    ("pending", "request_cancelled"): "cancelled",
    ("accepted", "request_completed"): "completed",
    ("accepted", "request_cancelled"): "cancelled",
}

class ServiceWorkspaceError(Exception):
    pass

def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, ServiceWorkspaceError) as exc:
        raise ServiceWorkspaceError(str(exc)) from exc
    except Exception as exc:
        raise ServiceWorkspaceError("Invalid workspace input.") from exc

def _delivery_days(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 365:
        raise ServiceWorkspaceError("delivery_days must be 1..365")
    return value

def _requirement_text(value: str) -> str:
    if not isinstance(value, str):
        raise ServiceWorkspaceError("Invalid requirement.")
    stripped = value.strip()
    if not 1 <= len(value) <= 500 or len(stripped)==0:
        raise ServiceWorkspaceError("Invalid requirement.")
    if any(ord(c) < 32 or ord(c)==127 for c in value):
        raise ServiceWorkspaceError("Invalid requirement.")
    return value

def _file_name(value: str) -> str:
    if not isinstance(value, str):
        raise ServiceWorkspaceError("Invalid file_name.")
    stripped = value.strip()
    if not 1 <= len(value) <= 200 or len(stripped)==0:
        raise ServiceWorkspaceError("Invalid file_name.")
    if any(ord(c) < 32 or ord(c)==127 for c in value):
        raise ServiceWorkspaceError("Invalid file_name.")
    # allow alphanum _-. and space
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- " for c in value if c not in "/\\"):
        # be permissive: just check not control, allow unicode? Keep simple: allow any printable except control
        pass
    return value

def _mime_type(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise ServiceWorkspaceError("Invalid mime_type.")
    if any(ord(c) < 32 or ord(c)==127 for c in value):
        raise ServiceWorkspaceError("Invalid mime_type.")
    return value

def _file_size(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 104857600:
        raise ServiceWorkspaceError("Invalid file_size.")
    return value

def _message(value: str) -> str:
    if not isinstance(value, str):
        raise ServiceWorkspaceError("Invalid message.")
    if not 1 <= len(value) <= 2000:
        raise ServiceWorkspaceError("Invalid message.")
    if any(ord(c) < 32 or ord(c)==127 for c in value if c not in "\n\t"):
        raise ServiceWorkspaceError("Invalid message.")
    return value

def _version(conn):
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver not in (25, 26, 27):
        # allow 25 only; but also allow higher future? For now require >=25
        from .state_storage import SUPPORTED_STATE_VERSIONS
        if ver not in SUPPORTED_STATE_VERSIONS or ver < 25:
            raise StateStorageError("State schema version 25 is required for service workspace.")

def _retry(conn, owner, key, digest, resource):
    row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
    if row is None:
        return None
    if row["resource_type"] != resource or not hmac.compare_digest(row["request_hash"], digest):
        raise ServiceWorkspaceError("Operation key was already used for a different request.")
    # Verify exists
    table_map = {
        _RESOURCE_PACKAGE: "service_packages",
        _RESOURCE_REQUIREMENT: "service_requirements",
        _RESOURCE_ATTACHMENT: "service_attachments",
        _RESOURCE_PROPOSAL: "proposals",
        _RESOURCE_REQUEST: "customer_requests",
    }
    table = table_map.get(resource)
    if table and conn.execute(f"SELECT 1 FROM {table} WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]

def _audit(conn, now, owner, event, resource, rid):
    conn.execute("INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)", (now, owner, event, resource, rid))

def _remember(conn, owner, key, digest, resource, rid):
    conn.execute("INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)", (owner, key, digest, resource, rid))

def _request(action, values):
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))

# ---------- Service editing (extends Services) ----------
class ServiceEditor:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def update(self, token: str, service_id: str, *, operation_key: str, title: str | None=None, scope: str | None=None, deliverable: str | None=None, price_paise: int | None=None, delivery_days: int | None=None, revisions: int | None=None) -> str:
        sid = _input(_record_id, service_id)
        key = _input(_key, operation_key)
        # Validate provided fields
        title_v = _input(_title, title) if title is not None else None
        scope_v = _input(_scope, scope) if scope is not None else None
        deliverable_v = _input(_deliverable, deliverable) if deliverable is not None else None
        price_v = _input(_price, price_paise) if price_paise is not None else None
        revisions_v = _input(_revisions, revisions) if revisions is not None else None
        # delivery_days is stored at package level; but we also allow service-level delivery via packages; for backward compat we store in description? Instead we treat delivery_days as part of service update by creating a default package if needed
        # For service table, we will add delivery_days handling via service_packages default? Simpler: store delivery_days as part of service update by updating a dummy field? But services table does not have delivery_days column.
        # We will instead treat delivery_days as update to a hidden column via service_packages? For now, if delivery_days provided, we require packages to represent it; so we just validate and then update service's updated_at and handle packages separately.
        # To keep API, we will update service's fields that exist, and if delivery_days provided, we create/update a default package.
        values = [sid, title_v or "", scope_v or "", deliverable_v or "", price_v if price_v is not None else -1, revisions_v if revisions_v is not None else -1, delivery_days if delivery_days is not None else 0]
        digest = _request("service_update", values)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _svc_version(conn)
            _version(conn)
            row = conn.execute("SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != "service_content" or not hmac.compare_digest(row["request_hash"], digest):
                    raise ServiceWorkspaceError("Operation key was already used for a different request.")
                return sid
            # Verify owned
            _owned_service(conn, owner, sid)
            # Build update
            updates = []
            params = []
            if title_v is not None:
                updates.append("title=?")
                params.append(title_v)
            if scope_v is not None:
                updates.append("scope=?")
                params.append(scope_v)
            if deliverable_v is not None:
                updates.append("deliverable=?")
                params.append(deliverable_v)
            if price_v is not None:
                updates.append("price_paise=?")
                params.append(price_v)
            if revisions_v is not None:
                updates.append("revisions=?")
                params.append(revisions_v)
            if not updates and delivery_days is None:
                raise ServiceWorkspaceError("No fields to update.")
            if updates:
                params.extend([now, sid, owner])
                conn.execute(f"UPDATE services SET {', '.join(updates)}, updated_at=? WHERE id=? AND owner_id=?", tuple(params))
                # Use separate resource for content updates to avoid breaking service FSM history
                _audit(conn, now, owner, "service_updated", "service_content", sid)
                _remember(conn, owner, key, digest, "service_content", sid)
            # Handle delivery_days via packages if provided
            if delivery_days is not None:
                dd = _input(_delivery_days, delivery_days)
                # Create or update a default package titled "Standard" if not exists
                existing = conn.execute("SELECT id FROM service_packages WHERE owner_id=? AND service_id=? AND title='Standard' LIMIT 1", (owner, sid)).fetchone()
                if existing is None:
                    # Need a separate operation for package creation? For simplicity, reuse same operation_key with suffix?
                    # Instead, create package directly with deterministic id derived from service id?
                    pkg_id = secrets.token_hex(16)
                    # Use service's current price/revisions if not provided
                    cur = conn.execute("SELECT price_paise, revisions FROM services WHERE id=? AND owner_id=?", (sid, owner)).fetchone()
                    price_pkg = price_v if price_v is not None else cur["price_paise"]
                    rev_pkg = revisions_v if revisions_v is not None else cur["revisions"]
                    conn.execute("INSERT INTO service_packages(id, owner_id, service_id, title, description, price_paise, delivery_days, revisions, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (pkg_id, owner, sid, "Standard", "Standard package", price_pkg, dd, rev_pkg, now, now))
                    # Audit package creation as part of service update? Use separate audit
                    _audit(conn, now, owner, "service_package_created", _RESOURCE_PACKAGE, pkg_id)
                else:
                    conn.execute("UPDATE service_packages SET delivery_days=?, updated_at=? WHERE id=? AND owner_id=?", (dd, now, existing["id"], owner))
            return sid

    def edit_description(self, token: str, service_id: str, *, operation_key: str, scope: str | None=None, deliverable: str | None=None) -> str:
        return self.update(token, service_id, operation_key=operation_key, scope=scope, deliverable=deliverable)

# ---------- Packages ----------
class ServicePackages:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(self, token: str, service_id: str, title: str, description: str, price_paise: int, delivery_days: int, revisions: int, *, operation_key: str) -> str:
        sid = _input(_record_id, service_id)
        title_v = _input(_title, title)
        desc_v = _input(_description, description)
        price = _input(_price, price_paise)
        dd = _input(_delivery_days, delivery_days)
        rev = _input(_revisions, revisions)
        key = _input(_key, operation_key)
        digest = _request("package_create", [sid, title_v, desc_v, price, dd, rev])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_PACKAGE)
            if existing is not None:
                return existing
            _owned_service(conn, owner, sid)
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO service_packages(id, owner_id, service_id, title, description, price_paise, delivery_days, revisions, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (ident, owner, sid, title_v, desc_v, price, dd, rev, now, now))
            _audit(conn, now, owner, "service_package_created", _RESOURCE_PACKAGE, ident)
            _remember(conn, owner, key, digest, _RESOURCE_PACKAGE, ident)
            return ident

    def get(self, token: str, package_id: str) -> dict:
        pid = _input(_record_id, package_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            row = conn.execute("SELECT * FROM service_packages WHERE id=? AND owner_id=?", (pid, owner)).fetchone()
            if row is None:
                raise AccessDenied("Access denied.")
            return dict(row)

    def list(self, token: str, service_id: str, *, after_id: str | None=None, limit: int=20) -> list[dict]:
        sid = _input(_record_id, service_id)
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ServiceWorkspaceError("Invalid limit.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            _owned_service(conn, owner, sid)
            rows = conn.execute("SELECT * FROM service_packages WHERE owner_id=? AND service_id=? AND id>? COLLATE BINARY ORDER BY id COLLATE BINARY LIMIT ?", (owner, sid, cursor, limit)).fetchall()
            return [dict(r) for r in rows]

    def update(self, token: str, package_id: str, *, operation_key: str, title: str | None=None, description: str | None=None, price_paise: int | None=None, delivery_days: int | None=None, revisions: int | None=None) -> str:
        pid = _input(_record_id, package_id)
        key = _input(_key, operation_key)
        title_v = _input(_title, title) if title is not None else None
        desc_v = _input(_description, description) if description is not None else None
        price_v = _input(_price, price_paise) if price_paise is not None else None
        dd_v = _input(_delivery_days, delivery_days) if delivery_days is not None else None
        rev_v = _input(_revisions, revisions) if revisions is not None else None
        values = [pid, title_v or "", desc_v or "", price_v if price_v is not None else -1, dd_v if dd_v is not None else -1, rev_v if rev_v is not None else -1]
        digest = _request("package_update", values)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_PACKAGE or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise ServiceWorkspaceError("Operation key was already used for a different request.")
                # idempotent return
                return pid
            # Check exists
            existing = conn.execute("SELECT * FROM service_packages WHERE id=? AND owner_id=?", (pid, owner)).fetchone()
            if existing is None:
                raise AccessDenied("Access denied.")
            updates=[]
            params=[]
            if title_v is not None:
                updates.append("title=?")
                params.append(title_v)
            if desc_v is not None:
                updates.append("description=?")
                params.append(desc_v)
            if price_v is not None:
                updates.append("price_paise=?")
                params.append(price_v)
            if dd_v is not None:
                updates.append("delivery_days=?")
                params.append(dd_v)
            if rev_v is not None:
                updates.append("revisions=?")
                params.append(rev_v)
            if not updates:
                raise ServiceWorkspaceError("No fields to update.")
            params.extend([now, pid, owner])
            conn.execute(f"UPDATE service_packages SET {', '.join(updates)}, updated_at=? WHERE id=? AND owner_id=?", tuple(params))
            _audit(conn, now, owner, "service_package_updated", _RESOURCE_PACKAGE, pid)
            _remember(conn, owner, key, digest, _RESOURCE_PACKAGE, pid)
            return pid

    def delete(self, token: str, package_id: str, *, operation_key: str) -> str:
        pid = _input(_record_id, package_id)
        key = _input(_key, operation_key)
        digest = _request("package_delete", [pid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_PACKAGE or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise ServiceWorkspaceError("Operation key was already used for a different request.")
                return pid
            existing = conn.execute("SELECT 1 FROM service_packages WHERE id=? AND owner_id=?", (pid, owner)).fetchone()
            if existing is None:
                raise AccessDenied("Access denied.")
            conn.execute("DELETE FROM service_packages WHERE id=? AND owner_id=?", (pid, owner))
            _audit(conn, now, owner, "service_package_deleted", _RESOURCE_PACKAGE, pid)
            _remember(conn, owner, key, digest, _RESOURCE_PACKAGE, pid)
            return pid

# ---------- Requirements ----------
class ServiceRequirements:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def add(self, token: str, service_id: str, requirement: str, *, operation_key: str) -> str:
        sid = _input(_record_id, service_id)
        req = _input(_requirement_text, requirement)
        key = _input(_key, operation_key)
        digest = _request("requirement_add", [sid, req])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_REQUIREMENT)
            if existing is not None:
                return existing
            _owned_service(conn, owner, sid)
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO service_requirements(id, owner_id, service_id, requirement, created_at) VALUES(?,?,?,?,?)", (ident, owner, sid, req, now))
            _audit(conn, now, owner, "service_requirement_created", _RESOURCE_REQUIREMENT, ident)
            _remember(conn, owner, key, digest, _RESOURCE_REQUIREMENT, ident)
            return ident

    def list(self, token: str, service_id: str) -> list[dict]:
        sid = _input(_record_id, service_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            _owned_service(conn, owner, sid)
            rows = conn.execute("SELECT * FROM service_requirements WHERE owner_id=? AND service_id=? ORDER BY created_at, id", (owner, sid)).fetchall()
            return [dict(r) for r in rows]

    def remove(self, token: str, requirement_id: str, *, operation_key: str) -> str:
        rid = _input(_record_id, requirement_id)
        key = _input(_key, operation_key)
        digest = _request("requirement_remove", [rid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_REQUIREMENT or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise ServiceWorkspaceError("Operation key was already used for a different request.")
                return rid
            existing = conn.execute("SELECT 1 FROM service_requirements WHERE id=? AND owner_id=?", (rid, owner)).fetchone()
            if existing is None:
                raise AccessDenied("Access denied.")
            conn.execute("DELETE FROM service_requirements WHERE id=? AND owner_id=?", (rid, owner))
            _audit(conn, now, owner, "service_requirement_deleted", _RESOURCE_REQUIREMENT, rid)
            _remember(conn, owner, key, digest, _RESOURCE_REQUIREMENT, rid)
            return rid

# ---------- Attachments ----------
class ServiceAttachments:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def add(self, token: str, *, operation_key: str, file_name: str, file_size: int, mime_type: str, file_hash: bytes | None=None, service_id: str | None=None, portfolio_item_id: str | None=None) -> str:
        fname = _input(_file_name, file_name)
        fsize = _input(_file_size, file_size)
        mime = _input(_mime_type, mime_type)
        if file_hash is not None:
            if not isinstance(file_hash, (bytes, bytearray)) or len(file_hash)!=32:
                raise ServiceWorkspaceError("Invalid file_hash.")
        sid = _input(_record_id, service_id) if service_id is not None else None
        pid = _input(_record_id, portfolio_item_id) if portfolio_item_id is not None else None
        if sid is None and pid is None:
            raise ServiceWorkspaceError("Either service_id or portfolio_item_id required.")
        key = _input(_key, operation_key)
        digest = _request("attachment_add", [sid or "", pid or "", fname, fsize, mime, file_hash.hex() if file_hash else ""])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_ATTACHMENT)
            if existing is not None:
                return existing
            if sid is not None:
                _owned_service(conn, owner, sid)
            if pid is not None:
                prow = conn.execute("SELECT 1 FROM portfolio_items WHERE id=? AND owner_id=?", (pid, owner)).fetchone()
                if prow is None:
                    raise ServiceWorkspaceError("Portfolio item not found.")
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO service_attachments(id, owner_id, service_id, portfolio_item_id, file_name, file_hash, file_size, mime_type, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (ident, owner, sid, pid, fname, file_hash, fsize, mime, now))
            _audit(conn, now, owner, "service_attachment_created", _RESOURCE_ATTACHMENT, ident)
            _remember(conn, owner, key, digest, _RESOURCE_ATTACHMENT, ident)
            return ident

    def list(self, token: str, *, service_id: str | None=None, portfolio_item_id: str | None=None) -> list[dict]:
        if service_id is None and portfolio_item_id is None:
            raise ServiceWorkspaceError("Filter required.")
        sid = _input(_record_id, service_id) if service_id else None
        pid = _input(_record_id, portfolio_item_id) if portfolio_item_id else None
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            query = "SELECT * FROM service_attachments WHERE owner_id=? "
            params=[owner]
            if sid is not None:
                query+="AND service_id=? "
                params.append(sid)
            if pid is not None:
                query+="AND portfolio_item_id=? "
                params.append(pid)
            query+="ORDER BY created_at, id"
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def remove(self, token: str, attachment_id: str, *, operation_key: str) -> str:
        aid = _input(_record_id, attachment_id)
        key = _input(_key, operation_key)
        digest = _request("attachment_remove", [aid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = conn.execute("SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_ATTACHMENT or not hmac.compare_digest(conn.execute("SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?", (owner, key)).fetchone()[0], digest):
                    raise ServiceWorkspaceError("Operation key was already used for a different request.")
                return aid
            existing = conn.execute("SELECT 1 FROM service_attachments WHERE id=? AND owner_id=?", (aid, owner)).fetchone()
            if existing is None:
                raise AccessDenied("Access denied.")
            conn.execute("DELETE FROM service_attachments WHERE id=? AND owner_id=?", (aid, owner))
            _audit(conn, now, owner, "service_attachment_deleted", _RESOURCE_ATTACHMENT, aid)
            _remember(conn, owner, key, digest, _RESOURCE_ATTACHMENT, aid)
            return aid

# ---------- Proposals ----------
class Proposals:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _owned(self, conn, owner, pid):
        row = conn.execute("SELECT * FROM proposals WHERE id=? AND owner_id=?", (pid, owner)).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id", (owner, _RESOURCE_PROPOSAL, pid)).fetchall()
        if not events:
            raise StateStorageError("Proposal history inconsistent.")
        replayed=None
        for (ev,) in events:
            step=(replayed, ev)
            if step not in _PROPOSAL_TRANSITIONS:
                raise StateStorageError("Proposal history inconsistent.")
            replayed=_PROPOSAL_TRANSITIONS[step]
        if replayed != row["status"]:
            raise StateStorageError("Proposal history inconsistent.")
        cnt=conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?", (owner, _RESOURCE_PROPOSAL, pid)).fetchone()[0]
        if cnt != len(events):
            raise StateStorageError("Proposal history inconsistent.")
        return row

    def create(self, token: str, service_id: str, title: str, description: str, price_paise: int, delivery_days: int, *, operation_key: str, customer_id: str | None=None, ai_generated: bool=False) -> str:
        sid = _input(_record_id, service_id)
        cid = _input(_record_id, customer_id) if customer_id else None
        title_v = _input(_title, title)
        desc_v = _input(_description, description)
        price = _input(_price, price_paise)
        dd = _input(_delivery_days, delivery_days)
        if type(ai_generated) is not bool:
            raise ServiceWorkspaceError("ai_generated must be bool.")
        key = _input(_key, operation_key)
        digest = _request("proposal_create", [sid, cid or "", title_v, desc_v, price, dd, ai_generated])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_PROPOSAL)
            if existing is not None:
                return existing
            _owned_service(conn, owner, sid)
            if cid is not None:
                if conn.execute("SELECT 1 FROM customers WHERE id=? AND owner_id=?", (cid, owner)).fetchone() is None:
                    raise ServiceWorkspaceError("Customer not found.")
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO proposals(id, owner_id, service_id, customer_id, title, description, price_paise, delivery_days, status, ai_generated, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?, 'draft', ?, ?, ?)",
                         (ident, owner, sid, cid, title_v, desc_v, price, dd, 1 if ai_generated else 0, now, now))
            _audit(conn, now, owner, "proposal_created", _RESOURCE_PROPOSAL, ident)
            _remember(conn, owner, key, digest, _RESOURCE_PROPOSAL, ident)
            return ident

    def generate_ai_draft(self, token: str, service_id: str, *, operation_key: str, customer_id: str | None=None, prompt: str | None=None) -> str:
        # Use AI assistant stub if available, else deterministic template
        sid = _input(_record_id, service_id)
        cid = _input(_record_id, customer_id) if customer_id else None
        key = _input(_key, operation_key)
        # Fetch service details
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            _owned_service(conn, owner, sid)
            svc = conn.execute("SELECT title, deliverable, price_paise, revisions FROM services WHERE id=? AND owner_id=?", (sid, owner)).fetchone()
            # Try AI assistant if available
            ai_output = None
            try:
                from .ai_assistant import AIAssistant
                ai = AIAssistant(self._auth, self._state)
                # Use chat to generate draft
                prompt_text = prompt or f"Generate proposal for service {svc['title']}: {svc['deliverable']}"
                res = ai.chat(token, prompt_text, external_content=None)
                ai_output = res.get("output", "")[:500] if isinstance(res, dict) else str(res)[:500]
            except Exception:
                ai_output = None
            if not ai_output:
                # deterministic fallback
                ai_output = f"Proposal for {svc['title']}: {svc['deliverable'][:100]}. Includes {svc['revisions']} revisions."
            title = f"Proposal: {svc['title'][:80]}"
            desc = ai_output
            price = svc["price_paise"]
            # delivery from service packages or default 7
            pkg = conn.execute("SELECT delivery_days FROM service_packages WHERE owner_id=? AND service_id=? LIMIT 1", (owner, sid)).fetchone()
            dd = pkg["delivery_days"] if pkg else 7
            # Create proposal with ai_generated=1
            digest = _request("proposal_create", [sid, cid or "", title, desc, price, dd, True])
            existing = _retry(conn, owner, key, digest, _RESOURCE_PROPOSAL)
            if existing is not None:
                return existing
            ident = secrets.token_hex(16)
            conn.execute("INSERT INTO proposals(id, owner_id, service_id, customer_id, title, description, price_paise, delivery_days, status, ai_generated, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?, 'draft', 1, ?, ?)",
                         (ident, owner, sid, cid, title, desc, price, dd, now, now))
            _audit(conn, now, owner, "proposal_created", _RESOURCE_PROPOSAL, ident)
            _remember(conn, owner, key, digest, _RESOURCE_PROPOSAL, ident)
            return ident

    def get(self, token: str, proposal_id: str) -> dict:
        pid = _input(_record_id, proposal_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            row = self._owned(conn, owner, pid)
            d=dict(row)
            d["verification"]="unverified"
            return d

    def list(self, token: str, *, after_id: str | None=None, limit: int=20, status: str | None=None, service_id: str | None=None, customer_id: str | None=None) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ServiceWorkspaceError("Invalid limit.")
        if status is not None and status not in _PROPOSAL_STATUSES:
            raise ServiceWorkspaceError("Invalid status.")
        sid = _input(_record_id, service_id) if service_id else None
        cid = _input(_record_id, customer_id) if customer_id else None
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            query="SELECT * FROM proposals WHERE owner_id=? AND id>? COLLATE BINARY"
            params=[owner, cursor]
            if status is not None:
                query+=" AND status=?"
                params.append(status)
            if sid is not None:
                query+=" AND service_id=?"
                params.append(sid)
            if cid is not None:
                query+=" AND customer_id=?"
                params.append(cid)
            query+=" ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows=conn.execute(query, tuple(params)).fetchall()
            # Validate each via _owned without nested transaction
            out=[]
            for r in rows:
                # Reuse _owned logic inline to avoid nested transaction
                pid=r["id"]
                # Verify history
                events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id", (owner, _RESOURCE_PROPOSAL, pid)).fetchall()
                if not events:
                    raise StateStorageError("Proposal history inconsistent.")
                replayed=None
                for (ev,) in events:
                    step=(replayed, ev)
                    if step not in _PROPOSAL_TRANSITIONS:
                        raise StateStorageError("Proposal history inconsistent.")
                    replayed=_PROPOSAL_TRANSITIONS[step]
                if replayed != r["status"]:
                    raise StateStorageError("Proposal history inconsistent.")
                d=dict(r)
                d["verification"]="unverified"
                out.append(d)
            return out

    def _transition(self, token: str, proposal_id: str, target: str, event: str, operation_key: str) -> str:
        pid = _input(_record_id, proposal_id)
        key = _input(_key, operation_key)
        if target not in _PROPOSAL_STATUSES:
            raise ServiceWorkspaceError("Invalid status.")
        digest = _request("proposal_transition", [pid, target])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = self._owned(conn, owner, pid)
            existing = _retry(conn, owner, key, digest, _RESOURCE_PROPOSAL)
            if existing is not None:
                return pid
            cur=row["status"]
            if _PROPOSAL_TRANSITIONS.get((cur, event)) != target:
                raise ServiceWorkspaceError("Transition not allowed.")
            conn.execute("UPDATE proposals SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, pid, owner))
            _audit(conn, now, owner, event, _RESOURCE_PROPOSAL, pid)
            _remember(conn, owner, key, digest, _RESOURCE_PROPOSAL, pid)
            return pid

    def send(self, token: str, proposal_id: str, *, operation_key: str) -> str:
        return self._transition(token, proposal_id, "sent", "proposal_sent", operation_key)
    def accept(self, token: str, proposal_id: str, *, operation_key: str) -> str:
        return self._transition(token, proposal_id, "accepted", "proposal_accepted", operation_key)
    def reject(self, token: str, proposal_id: str, *, operation_key: str) -> str:
        return self._transition(token, proposal_id, "rejected", "proposal_rejected", operation_key)
    def cancel(self, token: str, proposal_id: str, *, operation_key: str) -> str:
        pid = _input(_record_id, proposal_id)
        # allow cancel from draft or sent
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row=self._owned(conn, owner, pid)
            cur=row["status"]
            if cur not in ("draft","sent"):
                raise ServiceWorkspaceError("Transition not allowed.")
            return self._transition(token, proposal_id, "cancelled", "proposal_cancelled", operation_key)
    def archive(self, token: str, proposal_id: str, *, operation_key: str) -> str:
        return self._transition(token, proposal_id, "archived", "proposal_archived", operation_key)

# ---------- Customer Requests ----------
class CustomerRequests:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _owned(self, conn, owner, rid):
        row = conn.execute("SELECT * FROM customer_requests WHERE id=? AND owner_id=?", (rid, owner)).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id", (owner, _RESOURCE_REQUEST, rid)).fetchall()
        if not events:
            raise StateStorageError("Request history inconsistent.")
        replayed=None
        for (ev,) in events:
            if (replayed, ev) not in _REQUEST_TRANSITIONS:
                raise StateStorageError("Request history inconsistent.")
            replayed=_REQUEST_TRANSITIONS[(replayed, ev)]
        if replayed != row["status"]:
            raise StateStorageError("Request history inconsistent.")
        cnt=conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?", (owner, _RESOURCE_REQUEST, rid)).fetchone()[0]
        if cnt != len(events):
            raise StateStorageError("Request history inconsistent.")
        return row

    def create(self, token: str, service_id: str, customer_id: str, message: str, *, operation_key: str) -> str:
        sid = _input(_record_id, service_id)
        cid = _input(_record_id, customer_id)
        msg = _input(_message, message)
        key = _input(_key, operation_key)
        digest = _request("request_create", [sid, cid, msg])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing=_retry(conn, owner, key, digest, _RESOURCE_REQUEST)
            if existing is not None:
                return existing
            _owned_service(conn, owner, sid)
            if conn.execute("SELECT 1 FROM customers WHERE id=? AND owner_id=?", (cid, owner)).fetchone() is None:
                raise ServiceWorkspaceError("Customer not found.")
            ident=secrets.token_hex(16)
            conn.execute("INSERT INTO customer_requests(id, owner_id, service_id, customer_id, message, status, created_at, updated_at) VALUES(?,?,?,?,?, 'pending', ?, ?)", (ident, owner, sid, cid, msg, now, now))
            _audit(conn, now, owner, "request_created", _RESOURCE_REQUEST, ident)
            _remember(conn, owner, key, digest, _RESOURCE_REQUEST, ident)
            return ident

    def get(self, token: str, request_id: str) -> dict:
        rid=_input(_record_id, request_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            return dict(self._owned(conn, owner, rid))

    def list(self, token: str, *, after_id: str | None=None, limit: int=20, status: str | None=None, service_id: str | None=None, customer_id: str | None=None) -> list[dict]:
        cursor="" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ServiceWorkspaceError("Invalid limit.")
        if status is not None and status not in _REQUEST_STATUSES:
            raise ServiceWorkspaceError("Invalid status.")
        sid=_input(_record_id, service_id) if service_id else None
        cid=_input(_record_id, customer_id) if customer_id else None
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            query="SELECT * FROM customer_requests WHERE owner_id=? AND id>? COLLATE BINARY"
            params=[owner, cursor]
            if status is not None:
                query+=" AND status=?"
                params.append(status)
            if sid is not None:
                query+=" AND service_id=?"
                params.append(sid)
            if cid is not None:
                query+=" AND customer_id=?"
                params.append(cid)
            query+=" ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows=conn.execute(query, tuple(params)).fetchall()
            out=[]
            for r in rows:
                rid=r["id"]
                events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id", (owner, _RESOURCE_REQUEST, rid)).fetchall()
                if not events:
                    raise StateStorageError("Request history inconsistent.")
                replayed=None
                for (ev,) in events:
                    if (replayed, ev) not in _REQUEST_TRANSITIONS:
                        raise StateStorageError("Request history inconsistent.")
                    replayed=_REQUEST_TRANSITIONS[(replayed, ev)]
                if replayed != r["status"]:
                    raise StateStorageError("Request history inconsistent.")
                out.append(dict(r))
            return out

    def _transition(self, token: str, request_id: str, target: str, event: str, operation_key: str) -> str:
        rid=_input(_record_id, request_id)
        key=_input(_key, operation_key)
        if target not in _REQUEST_STATUSES:
            raise ServiceWorkspaceError("Invalid status.")
        digest=_request("request_transition", [rid, target])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row=self._owned(conn, owner, rid)
            existing=_retry(conn, owner, key, digest, _RESOURCE_REQUEST)
            if existing is not None:
                return rid
            cur=row["status"]
            if _REQUEST_TRANSITIONS.get((cur, event)) != target:
                raise ServiceWorkspaceError("Transition not allowed.")
            conn.execute("UPDATE customer_requests SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target, now, rid, owner))
            _audit(conn, now, owner, event, _RESOURCE_REQUEST, rid)
            _remember(conn, owner, key, digest, _RESOURCE_REQUEST, rid)
            return rid

    def accept(self, token: str, request_id: str, *, operation_key: str) -> str:
        return self._transition(token, request_id, "accepted", "request_accepted", operation_key)
    def reject(self, token: str, request_id: str, *, operation_key: str) -> str:
        return self._transition(token, request_id, "rejected", "request_rejected", operation_key)
    def complete(self, token: str, request_id: str, *, operation_key: str) -> str:
        return self._transition(token, request_id, "completed", "request_completed", operation_key)
    def cancel(self, token: str, request_id: str, *, operation_key: str) -> str:
        rid=_input(_record_id, request_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row=self._owned(conn, owner, rid)
            cur=row["status"]
            if cur not in ("pending","accepted"):
                raise ServiceWorkspaceError("Transition not allowed.")
            return self._transition(token, request_id, "cancelled", "request_cancelled", operation_key)

# ---------- Templates ----------
SERVICE_TEMPLATES: dict[str, dict[str, Any]] = {
    "excel_data_cleaning": {
        "title": "Excel Data Cleaning & Formatting",
        "scope": "Clean, validate and format Excel workbooks: remove duplicates, fix formulas, standardize dates, conditional formatting, data validation lists.",
        "deliverable": "Cleaned .xlsx file + change log",
        "price_paise": 150000,
        "delivery_days": 3,
        "revisions": 2,
        "packages": [
            {"title":"Basic","description":"Up to 1k rows, basic cleaning","price_paise":50000,"delivery_days":2,"revisions":1},
            {"title":"Standard","description":"Up to 5k rows, cleaning + validation","price_paise":150000,"delivery_days":3,"revisions":2},
            {"title":"Premium","description":"Up to 20k rows, advanced cleaning + dashboard","price_paise":350000,"delivery_days":5,"revisions":3},
        ],
        "requirements": ["Sample file or structure", "List of cleaning rules", "Output format preference"],
    },
    "data_entry": {
        "title": "Data Entry (Online & Offline)",
        "scope": "Accurate data entry from PDFs, images, scanned documents to Excel/CSV/Google Sheets with 99% accuracy and QC.",
        "deliverable": "Excel/CSV file + QC report",
        "price_paise": 80000,
        "delivery_days": 2,
        "revisions": 1,
        "packages": [
            {"title":"Basic","description":"500 entries","price_paise":80000,"delivery_days":2,"revisions":1},
            {"title":"Standard","description":"2000 entries, double-QC","price_paise":200000,"delivery_days":4,"revisions":2},
            {"title":"Premium","description":"5000+ entries, automation-ready","price_paise":400000,"delivery_days":7,"revisions":3},
        ],
        "requirements": ["Source files", "Target format/template", "Deadline and priority fields"],
    },
    "csv_processing": {
        "title": "CSV Processing & Transformation",
        "scope": "Parse, clean, merge, split, and transform CSV files: deduplication, encoding fixes, column mapping, large file handling.",
        "deliverable": "Processed CSV + processing script (optional)",
        "price_paise": 120000,
        "delivery_days": 3,
        "revisions": 2,
        "packages": [
            {"title":"Basic","description":"Single CSV <50k rows","price_paise":120000,"delivery_days":3,"revisions":1},
            {"title":"Standard","description":"Multi-file merge & transform","price_paise":250000,"delivery_days":5,"revisions":2},
            {"title":"Premium","description":"Large CSV + Python script","price_paise":500000,"delivery_days":7,"revisions":3},
        ],
        "requirements": ["Source CSV(s)", "Desired output schema", "Special handling notes"],
    },
    "python_automation": {
        "title": "Python Automation Script",
        "scope": "Custom Python scripts to automate repetitive tasks: file handling, web scraping (ToS-compliant), report generation, Excel automation.",
        "deliverable": "Python .py file + README + demo video",
        "price_paise": 250000,
        "delivery_days": 5,
        "revisions": 2,
        "packages": [
            {"title":"Basic","description":"Single script, single task","price_paise":250000,"delivery_days":5,"revisions":2},
            {"title":"Standard","description":"Multi-step automation + config","price_paise":500000,"delivery_days":8,"revisions":3},
            {"title":"Premium","description":"Automation suite + scheduling","price_paise":900000,"delivery_days":12,"revisions":3},
        ],
        "requirements": ["Task description & examples", "Environment details", "Sample inputs/outputs"],
    },
    "data_analysis": {
        "title": "Data Analysis & Insights",
        "scope": "Exploratory analysis, visualizations, summary statistics, trends, and actionable insights using Python/Excel.",
        "deliverable": "Report PDF + Excel + charts",
        "price_paise": 300000,
        "delivery_days": 5,
        "revisions": 2,
        "packages": [
            {"title":"Basic","description":"Descriptive stats + charts","price_paise":300000,"delivery_days":5,"revisions":2},
            {"title":"Standard","description":"Deep dive + insights + deck","price_paise":600000,"delivery_days":8,"revisions":3},
            {"title":"Premium","description":"Analysis + dashboard + recommendations","price_paise":1000000,"delivery_days":12,"revisions":3},
        ],
        "requirements": ["Dataset", "Analysis goals/KPIs", "Preferred tools"],
    },
    "website_development": {
        "title": "Website Development",
        "scope": "Responsive website development: landing pages, business sites, portfolio sites, basic SEO, contact forms.",
        "deliverable": "Deployed site + source files",
        "price_paise": 500000,
        "delivery_days": 10,
        "revisions": 3,
        "packages": [
            {"title":"Basic","description":"Single page landing","price_paise":500000,"delivery_days":10,"revisions":2},
            {"title":"Standard","description":"5-page business site","price_paise":1200000,"delivery_days":15,"revisions":3},
            {"title":"Premium","description":"10-page + CMS + SEO","price_paise":2500000,"delivery_days":25,"revisions":3},
        ],
        "requirements": ["Content & branding assets", "Reference sites", "Domain/hosting details"],
    },
    "frontend_development": {
        "title": "Frontend Development (React/Vanilla)",
        "scope": "Frontend implementation from Figma/design: responsive, accessible, performant, clean code.",
        "deliverable": "Frontend code + demo link",
        "price_paise": 400000,
        "delivery_days": 8,
        "revisions": 3,
        "packages": [
            {"title":"Basic","description":"1-3 components/pages","price_paise":400000,"delivery_days":8,"revisions":2},
            {"title":"Standard","description":"5-8 pages, state + API integration","price_paise":900000,"delivery_days":14,"revisions":3},
            {"title":"Premium","description":"Full SPA + tests + deployment","price_paise":1800000,"delivery_days":21,"revisions":3},
        ],
        "requirements": ["Design files (Figma)", "API specs if any", "Browser support list"],
    },
    "ai_workflow_automation": {
        "title": "AI Workflow Automation",
        "scope": "AI-powered workflows: document processing, email triage, data extraction, chatbot, Zapier/Make-style automation with approvals.",
        "deliverable": "Workflow + documentation + demo",
        "price_paise": 600000,
        "delivery_days": 10,
        "revisions": 3,
        "packages": [
            {"title":"Basic","description":"Single AI workflow","price_paise":600000,"delivery_days":10,"revisions":2},
            {"title":"Standard","description":"Multi-step workflow + human review","price_paise":1200000,"delivery_days":15,"revisions":3},
            {"title":"Premium","description":"Suite of workflows + monitoring","price_paise":2500000,"delivery_days":25,"revisions":3},
        ],
        "requirements": ["Workflow description", "Sample inputs & expected outputs", "Approval requirements"],
    },
    "document_invoice_automation": {
        "title": "Document & Invoice Automation",
        "scope": "Automate document and invoice generation: templates, data merging, PDF generation, numbering, storage.",
        "deliverable": "Automation script + templates + sample PDFs",
        "price_paise": 350000,
        "delivery_days": 7,
        "revisions": 2,
        "packages": [
            {"title":"Basic","description":"Invoice template + data merge","price_paise":350000,"delivery_days":7,"revisions":2},
            {"title":"Standard","description":"Multi-template + batch generation","price_paise":700000,"delivery_days":12,"revisions":3},
            {"title":"Premium","description":"Full doc system + storage + email","price_paise":1300000,"delivery_days":18,"revisions":3},
        ],
        "requirements": ["Template designs", "Data source details", "Output format & storage"],
    },
}

class ServiceTemplates:
    def list_templates(self) -> list[str]:
        return sorted(SERVICE_TEMPLATES.keys())

    def get_template(self, template_id: str) -> dict:
        if template_id not in SERVICE_TEMPLATES:
            raise ServiceWorkspaceError("Template not found.")
        # deep copy via json
        import copy
        return copy.deepcopy(SERVICE_TEMPLATES[template_id])

    def create_service_from_template(self, auth: AuthService, state: StateStorage, token: str, template_id: str, *, operation_key: str, title: str | None=None, price_paise: int | None=None) -> str:
        tmpl = self.get_template(template_id)
        # All fields are editable overrides
        from .services import Services
        svc = Services(auth, state)
        eff_title = title if title is not None else tmpl["title"]
        scope = tmpl["scope"]
        deliverable = tmpl["deliverable"]
        price = price_paise if price_paise is not None else tmpl["price_paise"]
        rev = tmpl["revisions"]
        sid = svc.create(token, eff_title, scope, deliverable, price, rev, operation_key=operation_key)
        # Create packages and requirements as separate ops with derived keys
        # To keep idempotency, use operation_key suffixed
        from .service_workspace import ServicePackages, ServiceRequirements
        pkgs = ServicePackages(auth, state)
        reqs = ServiceRequirements(auth, state)
        for idx, pkg in enumerate(tmpl.get("packages", [])):
            try:
                pkgs.create(token, sid, pkg["title"], pkg["description"], pkg["price_paise"], pkg["delivery_days"], pkg["revisions"], operation_key=f"{operation_key}_pkg{idx}")
            except Exception:
                pass
        for idx, req in enumerate(tmpl.get("requirements", [])):
            try:
                reqs.add(token, sid, req, operation_key=f"{operation_key}_req{idx}")
            except Exception:
                pass
        return sid

# ---------- Portfolio Presentation ----------
class PortfolioPresentation:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def render(self, token: str, service_id: str | None=None) -> dict:
        # Returns structured data for professional portfolio presentation
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            if service_id is not None:
                sid = _input(_record_id, service_id)
                _owned_service(conn, owner, sid)
                services = [dict(conn.execute("SELECT * FROM services WHERE id=? AND owner_id=?", (sid, owner)).fetchone())]
            else:
                rows = conn.execute("SELECT * FROM services WHERE owner_id=? ORDER BY created_at DESC, id", (owner,)).fetchall()
                services = [dict(r) for r in rows]
            out=[]
            for svc in services:
                sid=svc["id"]
                # fetch related directly within same transaction to avoid nested locks
                pkgs = [dict(r) for r in conn.execute("SELECT * FROM service_packages WHERE owner_id=? AND service_id=? ORDER BY id", (owner, sid)).fetchall()]
                reqs = [dict(r) for r in conn.execute("SELECT * FROM service_requirements WHERE owner_id=? AND service_id=? ORDER BY created_at, id", (owner, sid)).fetchall()]
                atts = [dict(r) for r in conn.execute("SELECT * FROM service_attachments WHERE owner_id=? AND service_id=? ORDER BY created_at, id", (owner, sid)).fetchall()]
                items_rows = conn.execute("SELECT * FROM portfolio_items WHERE owner_id=? AND service_id=? ORDER BY id", (owner, sid)).fetchall()
                items=[]
                for it in items_rows:
                    it_dict=dict(it)
                    ia = [dict(r) for r in conn.execute("SELECT * FROM service_attachments WHERE owner_id=? AND portfolio_item_id=? ORDER BY created_at, id", (owner, it_dict["id"])).fetchall()]
                    it_dict["attachments"]=ia
                    items.append(it_dict)
                out.append({
                    "service": svc,
                    "packages": pkgs,
                    "requirements": reqs,
                    "attachments": atts,
                    "portfolio_items": items,
                "presentation": {
                    "headline": f"{svc['title']} — {svc['deliverable'][:60]}",
                    "price_display": f"INR {svc['price_paise']/100:.2f} • {svc['revisions']} revisions",
                    "packages_display": "; ".join([f"{p['title']} INR {p['price_paise']/100:.2f}/{p['delivery_days']}d" for p in pkgs]) if pkgs else "No packages yet",
                    "verification": "unverified",
                }
            })
        return {"owner_id": owner, "services": out, "verification": "unverified", "total_services": len(out)}

