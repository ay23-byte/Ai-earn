"""Modular payment-provider sandbox (M9) — Razorpay/Stripe shaped, HMAC verified, idempotent, auditable.

No real provider network, no card storage, no live money. Sandbox orders are local
records; live mode is blocked without explicit approval and verified credentials.
All amounts are INR paise 0..1e12. Uses session-owned _authorized_transaction,
state_operations idempotency, state_audit history replay, and backup-checked integrity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE = "provider_order"
_PROVIDERS = frozenset({"razorpay", "stripe"})
_MODES = frozenset({"sandbox", "live"})
_STATUSES = frozenset({"created", "authorized", "captured", "settled", "refunded", "failed"})
_TRANSITIONS = {
    (None, "provider_order_created"): "created",
    ("created", "provider_order_authorized"): "authorized",
    ("created", "provider_order_failed"): "failed",
    ("authorized", "provider_order_captured"): "captured",
    ("authorized", "provider_order_failed"): "failed",
    ("captured", "provider_order_settled"): "settled",
    ("captured", "provider_order_refunded"): "refunded",
    ("settled", "provider_order_refunded"): "refunded",
    ("captured", "provider_order_failed"): "failed",
}
_EVENT_TO_STATUS = {
    "authorized": "authorized",
    "captured": "captured",
    "settled": "settled",
    "refunded": "refunded",
    "failed": "failed",
}
_EVENT_TYPES = frozenset(_EVENT_TO_STATUS.keys())
_SIGNATURE_HEX = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_PROVIDER_ORDER_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_TIME = 4_102_444_800


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    provider: str  # razorpay | stripe
    mode: str  # sandbox | live
    webhook_secret: bytes  # 32..128 bytes, never logged
    # api_key not stored; for sandbox we generate local orders, for live we would call provider

    def __post_init__(self):
        if self.provider not in _PROVIDERS:
            raise ProviderError("Invalid provider.")
        if self.mode not in _MODES:
            raise ProviderError("Invalid mode.")
        if type(self.webhook_secret) is not bytes or not 32 <= len(self.webhook_secret) <= 128:
            raise ProviderError("Invalid webhook secret.")
        if self.mode == "live":
            # Live is blocked until explicit approval + verified credentials (M11 gate)
            raise ProviderError("Live mode is not enabled; sandbox only until explicit approval.")


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, ProviderError) as exc:
        raise ProviderError(str(exc)) from exc
    except Exception as exc:
        raise ProviderError("Invalid provider input.") from exc


def _amount(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
        raise ProviderError("Invalid amount.")
    return value


def _provider(value: str) -> str:
    if value not in _PROVIDERS:
        raise ProviderError("Invalid provider.")
    return value


def _mode(value: str) -> str:
    if value not in _MODES:
        raise ProviderError("Invalid mode.")
    return value


def _version_check(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 21:
        raise StateStorageError("State schema version 21 is required for provider orders.")


def _retry(conn, owner, key, digest):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise ProviderError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM payment_provider_orders WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
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


def _object(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ProviderError("Invalid webhook event.")
        result[k] = v
    return result


def _invalid_constant(_v):
    raise ProviderError("Invalid webhook event.")


def _verify_razorpay_signature(secret: bytes, body: bytes, signature: str) -> None:
    if not isinstance(signature, str) or _SIGNATURE_HEX.fullmatch(signature) is None:
        raise ProviderError("Invalid webhook signature.")
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ProviderError("Invalid webhook signature.")


def _verify_stripe_signature(secret: bytes, body: bytes, signature: str, now: int) -> None:
    # Stripe header: t=timestamp,v1=hmac
    # hmac = HMAC-SHA256(secret, f"{t}.{raw_body}")
    if not isinstance(signature, str) or "," not in signature:
        raise ProviderError("Invalid webhook signature.")
    parts = {}
    for item in signature.split(","):
        if "=" not in item:
            raise ProviderError("Invalid webhook signature.")
        k, v = item.split("=", 1)
        parts[k] = v
    if "t" not in parts or "v1" not in parts:
        raise ProviderError("Invalid webhook signature.")
    try:
        t = int(parts["t"])
    except ValueError as exc:
        raise ProviderError("Invalid webhook signature.") from exc
    if abs(t - now) > 300:
        raise ProviderError("Invalid webhook event.")
    if _SIGNATURE_HEX.fullmatch(parts["v1"]) is None:
        raise ProviderError("Invalid webhook signature.")
    signed = f"{t}.{body.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts["v1"]):
        raise ProviderError("Invalid webhook signature.")


def _validate_webhook_body(provider: str, body: bytes, now: int) -> dict:
    if type(body) is not bytes or not 1 <= len(body) <= 8192:
        raise ProviderError("Invalid webhook event.")
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProviderError("Invalid webhook event.") from exc
    # Common required fields for our sandbox provider shape
    # We accept a minimal provider-shaped payload:
    # {"event_id": "...", "event_type": "captured|...", "provider_order_id": "order_...", "amount_paise": 50000, "currency": "INR", "occurred_at": 123456}
    # plus provider-specific wrappers are normalized outside.
    # For strictness, require exactly these keys (no extra).
    required = {"event_id", "event_type", "provider_order_id", "amount_paise", "currency", "occurred_at"}
    if type(data) is not dict or frozenset(data) != required:
        raise ProviderError("Invalid webhook event.")
    if not isinstance(data["event_id"], str) or _EVENT_ID_RE.fullmatch(data["event_id"]) is None:
        raise ProviderError("Invalid webhook event.")
    if data["event_type"] not in _EVENT_TYPES:
        raise ProviderError("Invalid webhook event.")
    if not isinstance(data["provider_order_id"], str) or _PROVIDER_ORDER_ID_RE.fullmatch(data["provider_order_id"]) is None:
        raise ProviderError("Invalid webhook event.")
    if type(data["amount_paise"]) is not int or not 0 <= data["amount_paise"] <= 1_000_000_000_000:
        raise ProviderError("Invalid webhook event.")
    if data["currency"] != "INR":
        raise ProviderError("Invalid webhook event.")
    if type(data["occurred_at"]) is not int or not 0 <= data["occurred_at"] <= _MAX_TIME or abs(data["occurred_at"] - now) > 300:
        raise ProviderError("Invalid webhook event.")
    # Provider field is not in body; caller supplies it
    return data


def _owned(conn, owner, record_id):
    row = conn.execute("SELECT * FROM payment_provider_orders WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE, record_id),
    ).fetchall()
    if not events or events[0][0] != "provider_order_created":
        raise StateStorageError("Provider order history is inconsistent.")
    # Replay
    current = None
    for ev in events:
        step = (current, ev[0])
        if step not in _TRANSITIONS:
            raise StateStorageError("Provider order history is inconsistent.")
        current = _TRANSITIONS[step]
    if current != row["status"]:
        raise StateStorageError("Provider order history is inconsistent.")
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Provider order history is inconsistent.")
    return row


class PaymentProviders:
    """Sandbox provider orders — HMAC verified, idempotent, audited, no live network."""

    def __init__(self, auth: AuthService, state: StateStorage, config: ProviderConfig):
        self._auth = auth
        self._state = state
        self._config = config

    def create_order(self, token: str, amount_paise: int, *, operation_key: str) -> str:
        amt = _input(_amount, amount_paise)
        key = _input(_key, operation_key)
        digest = _request("provider_order_create", [self._config.provider, self._config.mode, amt])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            ident = secrets.token_hex(16)
            provider_order_id = f"order_{ident[:16]}"
            if not _PROVIDER_ORDER_ID_RE.fullmatch(provider_order_id):
                raise ProviderError("Invalid provider order id.")
            # Ensure uniqueness per owner
            if conn.execute("SELECT 1 FROM payment_provider_orders WHERE provider_order_id=? AND owner_id=?", (provider_order_id, owner)).fetchone() is not None:
                provider_order_id = f"order_{secrets.token_hex(8)}"
            conn.execute(
                "INSERT INTO payment_provider_orders(id, owner_id, provider, mode, amount_paise, currency, status, provider_order_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, owner, self._config.provider, self._config.mode, amt, "INR", "created", provider_order_id, now, now),
            )
            _audit(conn, now, owner, "provider_order_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(_owned(conn, owner, ident))

    def list(self, token: str, *, after_id: str | None = None, limit: int = 20, provider: str | None = None, status: str | None = None) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProviderError("Invalid page size.")
        if provider is not None and provider not in _PROVIDERS:
            raise ProviderError("Invalid provider filter.")
        if status is not None and status not in _STATUSES:
            raise ProviderError("Invalid status filter.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT id FROM payment_provider_orders WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if provider is not None:
                query += " AND provider=?"
                params.append(provider)
            if status is not None:
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(_owned(conn, owner, r["id"])) for r in rows]

    def handle_webhook(self, token: str, raw_body: bytes, signature: str, *, operation_key: str) -> dict:
        """Verify HMAC over raw bytes, validate body, update order status idempotently.

        Returns {"event_id": ..., "order_id": ..., "new_status": ...}
        Never trusts provider status without verification and amount binding.
        """
        key = _input(_key, operation_key)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            # Verify signature over raw bytes before parsing
            if self._config.provider == "razorpay":
                _verify_razorpay_signature(self._config.webhook_secret, raw_body, signature)
            else:  # stripe
                _verify_stripe_signature(self._config.webhook_secret, raw_body, signature, now)
            data = _validate_webhook_body(self._config.provider, raw_body, now)
            body_hash = hashlib.sha256(raw_body).digest()
            digest = _request("provider_webhook", [data["event_id"], body_hash.hex()])
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                # Idempotent retry must map to same order; check event binding
                order_row = _owned(conn, owner, existing)
                if order_row["provider_order_id"] != data["provider_order_id"]:
                    raise StateStorageError("Stored operation result is inconsistent.")
                # Also check event table binding
                ev = conn.execute("SELECT order_id, raw_body_hash FROM payment_provider_events WHERE owner_id=? AND event_id=?", (owner, data["event_id"])).fetchone()
                if ev is None or ev["order_id"] != existing or not hmac.compare_digest(ev["raw_body_hash"], body_hash):
                    raise StateStorageError("Stored webhook event is inconsistent.")
                return {"event_id": data["event_id"], "order_id": existing, "new_status": order_row["status"]}
            # Lookup order by provider_order_id
            order = conn.execute("SELECT id, amount_paise, status FROM payment_provider_orders WHERE provider_order_id=? AND owner_id=?", (data["provider_order_id"], owner)).fetchone()
            if order is None:
                raise AccessDenied("Access denied.")
            if order["amount_paise"] != data["amount_paise"]:
                raise ProviderError("Webhook amount mismatch.")
            # Check event_id conflict with different body
            prior = conn.execute("SELECT order_id, raw_body_hash, event_type FROM payment_provider_events WHERE owner_id=? AND event_id=?", (owner, data["event_id"])).fetchone()
            if prior is not None:
                if prior["order_id"] != order["id"] or not hmac.compare_digest(prior["raw_body_hash"], body_hash) or prior["event_type"] != data["event_type"]:
                    raise ProviderError("Conflicting webhook event.")
                # Already processed, but operation_key is new -> treat as duplicate event_id with same body but new key: should be idempotent per event_id, but operation_key differs -> conflict per spec: operation_key must be unique, so this is a different request with same event_id -> we should return existing order status without new audit?
                # For strictness, we treat duplicate event_id as already handled: return current status, but also ensure operation_key not reused for different request (we already checked _retry miss, so this is a new key for same event -> we should not allow reuse of event_id under different operation_key; instead require same operation_key for retry.
                # To keep simple, raise conflict if event_id already exists under different operation_key
                raise ProviderError("Webhook event already processed.")
            # Validate transition
            current_status = order["status"]
            expected = _EVENT_TO_STATUS[data["event_type"]]
            transition_event = f"provider_order_{expected}"
            if (current_status, transition_event) not in _TRANSITIONS:
                raise ProviderError("Transition is not allowed.")
            # Re-verify freshness before commit (replay protection)
            if self._config.provider == "razorpay":
                _verify_razorpay_signature(self._config.webhook_secret, raw_body, signature)
            else:
                _verify_stripe_signature(self._config.webhook_secret, raw_body, signature, now)
            # Atomically record event and status change
            conn.execute("INSERT INTO payment_provider_events(owner_id, event_id, order_id, event_type, amount_paise, raw_body_hash, received_at) VALUES(?,?,?,?,?,?,?)",
                         (owner, data["event_id"], order["id"], data["event_type"], data["amount_paise"], body_hash, now))
            conn.execute("UPDATE payment_provider_orders SET status=?, updated_at=? WHERE id=? AND owner_id=?", (expected, now, order["id"], owner))
            _audit(conn, now, owner, transition_event, order["id"])
            _remember(conn, owner, key, digest, order["id"])
            return {"event_id": data["event_id"], "order_id": order["id"], "new_status": expected}

    def reconcile(self, token: str, record_id: str) -> dict:
        """Return order + event count + audit replay check — not provider fetch."""
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            events = conn.execute("SELECT event_id, event_type, amount_paise FROM payment_provider_events WHERE owner_id=? AND order_id=? ORDER BY received_at", (owner, ident)).fetchall()
            # No external fetch; compare stored status vs event replay
            # Already validated via _owned replay, so just return counts
            return {"order": dict(row), "event_count": len(events), "events": [dict(e) for e in events], "mode": row["mode"], "provider": row["provider"], "reconciliation": "local_only"}

