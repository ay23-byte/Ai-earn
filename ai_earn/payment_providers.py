"""Modular payment-provider sandbox — Phase 10.

Design: provider adapters for Razorpay/Stripe are isolated, share a base
contract. Sandbox orders are local records; live mode remains blocked until
explicit approval (M11). No network, no card storage, no live money.

Razorpay: HMAC-SHA256(raw_body, webhook_secret) hex → X-Razorpay-Signature
Stripe: HMAC-SHA256(t.raw_body, webhook_secret) hex → Stripe-Signature: t=..,v1=..
Both verified over raw bytes before JSON parse, constant-time compare, replay
window 300s, amount/currency/provider_order_id binding, owner-isolated,
idempotent via operation_key + event_id, audited, reconcilable.

Sandbox payload shape (minimal, strict):
  {"event_id","event_type","provider_order_id","amount_paise","currency","occurred_at"}
Real provider shapes are normalized to this shape for internal transitions;
unknown shapes are rejected. Sandbox and production configs are kept separate
by (provider,mode) and live is never enabled without explicit approval.
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
_MAX_BODY = 8192


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    """Sandbox-only config. Live is blocked; separate secrets per (provider,mode)."""

    provider: str  # razorpay | stripe
    mode: str  # sandbox | live
    webhook_secret: bytes  # 32..128 bytes, never logged

    def __post_init__(self):
        if self.provider not in _PROVIDERS:
            raise ProviderError("Invalid provider.")
        if self.mode not in _MODES:
            raise ProviderError("Invalid mode.")
        if type(self.webhook_secret) is not bytes or not 32 <= len(self.webhook_secret) <= 128:
            raise ProviderError("Invalid webhook secret.")
        if self.mode == "live":
            # Phase 11 gate: live remains blocked until production readiness checklist
            # (docs/PRODUCTION_READINESS.md) is signed and ai_earn/production_readiness.evaluate() passes.
            raise ProviderError("Live mode is not enabled; sandbox only until explicit approval. See docs/PRODUCTION_READINESS.md and ai_earn/production_readiness.evaluate().")


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


# ---------------------------------------------------------------------------
# Provider adapters — modular, per-provider verification & normalization
# ---------------------------------------------------------------------------


class _BaseAdapter:
    provider: str

    def verify(self, secret: bytes, body: bytes, signature: str, now: int) -> None:
        raise NotImplementedError

    def normalize(self, body: bytes) -> dict:
        """Parse raw body (already verified) into sandbox shape; strict."""
        raise NotImplementedError


class _RazorpayAdapter(_BaseAdapter):
    provider = "razorpay"

    def verify(self, secret: bytes, body: bytes, signature: str, now: int) -> None:
        # Per https://razorpay.com/docs/webhooks/validate-test : HMAC-SHA256(raw_body, secret) hex → X-Razorpay-Signature
        # Must use raw bytes, constant-time.
        if not isinstance(signature, str) or _SIGNATURE_HEX.fullmatch(signature) is None:
            raise ProviderError("Invalid webhook signature.")
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ProviderError("Invalid webhook signature.")

    def normalize(self, body: bytes) -> dict:
        # Sandbox shape is already minimal; also accept real Razorpay event shape by normalizing.
        # Real Razorpay webhook (India) e.g. {"entity":"event","event":"payment.captured","payload":{"payment":{"entity":"payment","order_id":"order_...","amount":50000,"currency":"INR","status":"captured"}},"created_at":123}
        # For sandbox we keep strict minimal shape; try minimal first, then attempt real-shape fallback.
        try:
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ProviderError("Invalid webhook event.") from exc
        # Minimal sandbox shape
        if isinstance(data, dict) and frozenset(data) == {"event_id", "event_type", "provider_order_id", "amount_paise", "currency", "occurred_at"}:
            return data
        # Try real Razorpay shape: map to sandbox shape
        if isinstance(data, dict) and data.get("entity") == "event" and "event" in data and "payload" in data:
            try:
                event = data["event"]  # e.g. payment.authorized -> authorized
                # Map known Razorpay events to our internal types
                mapping = {
                    "payment.authorized": "authorized",
                    "payment.captured": "captured",
                    "payment.failed": "failed",
                    "order.paid": "settled",
                    "refund.created": "refunded",
                    "refund.processed": "refunded",
                }
                et = mapping.get(event)
                if et is None or et not in _EVENT_TYPES:
                    raise ProviderError("Invalid webhook event.")
                payload = data["payload"]
                # Try to extract provider_order_id, amount, currency
                # Payment payload
                payment = None
                if isinstance(payload, dict):
                    # Could be {"payment": {...}} or {"order": {...}} etc.
                    if "payment" in payload and isinstance(payload["payment"], dict):
                        payment = payload["payment"]
                    elif "refund" in payload and isinstance(payload["refund"], dict):
                        payment = payload["refund"]
                    elif "order" in payload and isinstance(payload["order"], dict):
                        payment = payload["order"]
                if payment is None or not isinstance(payment, dict):
                    raise ProviderError("Invalid webhook event.")
                # Razorpay amounts are in paise already for INR?
                amt = payment.get("amount")
                if not isinstance(amt, int):
                    raise ProviderError("Invalid webhook event.")
                cur = payment.get("currency")
                if cur != "INR":
                    raise ProviderError("Invalid webhook event.")
                # order_id or id
                poid = payment.get("order_id") or payment.get("id") or data.get("id")
                if not isinstance(poid, str) or _PROVIDER_ORDER_ID_RE.fullmatch(poid) is None:
                    raise ProviderError("Invalid webhook event.")
                created = data.get("created_at")
                if not isinstance(created, int):
                    raise ProviderError("Invalid webhook event.")
                # event_id from Razorpay: use account_id+event or id
                eid = data.get("id") or payment.get("id") or "evt_real"
                if not isinstance(eid, str) or _EVENT_ID_RE.fullmatch(eid) is None:
                    eid = "evt_" + hashlib.sha256(body).hexdigest()[:16]
                return {
                    "event_id": eid,
                    "event_type": et,
                    "provider_order_id": poid,
                    "amount_paise": amt,
                    "currency": "INR",
                    "occurred_at": created,
                }
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError("Invalid webhook event.") from exc
        raise ProviderError("Invalid webhook event.")


class _StripeAdapter(_BaseAdapter):
    provider = "stripe"

    def verify(self, secret: bytes, body: bytes, signature: str, now: int) -> None:
        # Per https://stripe.com/docs/webhooks/signatures : Stripe-Signature: t=...,v1=...
        # signed_payload = t.body, HMAC-SHA256(secret, signed_payload) hex, compare to any v1, check timestamp 300s
        if not isinstance(signature, str) or not signature:
            raise ProviderError("Invalid webhook signature.")
        # Parse header: t=...,v1=...,v1=... (multiple v1 allowed for rotation)
        t = None
        v1s: list[str] = []
        for item in signature.split(","):
            item = item.strip()
            if not item or "=" not in item:
                raise ProviderError("Invalid webhook signature.")
            k, v = item.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k == "t":
                if t is not None:
                    raise ProviderError("Invalid webhook signature.")
                try:
                    t = int(v)
                except ValueError as exc:
                    raise ProviderError("Invalid webhook signature.") from exc
            elif k == "v1":
                if _SIGNATURE_HEX.fullmatch(v) is None:
                    raise ProviderError("Invalid webhook signature.")
                v1s.append(v)
            elif k == "v0":
                # Stripe may also send v0 (deprecated), ignore but validate format if present
                continue
            else:
                # Unknown scheme, ignore but must be well-formed
                continue
        if t is None or not v1s:
            raise ProviderError("Invalid webhook signature.")
        if abs(t - now) > 300:
            raise ProviderError("Invalid webhook event.")
        signed = f"{t}.{body.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        # Constant-time compare against any v1 (rotation)
        ok = any(hmac.compare_digest(expected, v) for v in v1s)
        if not ok:
            raise ProviderError("Invalid webhook signature.")

    def normalize(self, body: bytes) -> dict:
        try:
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ProviderError("Invalid webhook event.") from exc
        # Minimal sandbox shape
        if isinstance(data, dict) and frozenset(data) == {"event_id", "event_type", "provider_order_id", "amount_paise", "currency", "occurred_at"}:
            return data
        # Real Stripe shape: {"id":"evt_...","type":"payment_intent.succeeded","data":{"object":{"id":"pi_...","amount":50000,"currency":"inr"}},"created":123}
        if isinstance(data, dict) and "type" in data and "data" in data and isinstance(data["data"], dict):
            try:
                typ = data["type"]
                mapping = {
                    "payment_intent.amount_capturable_updated": "authorized",
                    "payment_intent.succeeded": "captured",
                    "payment_intent.payment_failed": "failed",
                    "charge.succeeded": "captured",
                    "charge.failed": "failed",
                    "charge.refunded": "refunded",
                    "refund.created": "refunded",
                    "payout.paid": "settled",
                }
                et = mapping.get(typ)
                if et is None or et not in _EVENT_TYPES:
                    raise ProviderError("Invalid webhook event.")
                obj = data["data"].get("object") if isinstance(data["data"].get("object"), dict) else {}
                amt = obj.get("amount")
                cur = obj.get("currency")
                # Stripe amounts are in smallest unit; for INR paise already
                if not isinstance(amt, int):
                    # Try amount_capturable etc.
                    amt = obj.get("amount_capturable") or obj.get("amount_received")
                if not isinstance(amt, int) or not 0 <= amt <= 1_000_000_000_000:
                    raise ProviderError("Invalid webhook event.")
                if not isinstance(cur, str) or cur.upper() != "INR":
                    # Stripe test mode may use 'usd' etc. but we require INR
                    if cur.lower() != "inr":
                        raise ProviderError("Invalid webhook event.")
                poid = obj.get("id") or data.get("id")
                if not isinstance(poid, str) or _PROVIDER_ORDER_ID_RE.fullmatch(poid) is None:
                    # Try order id from metadata
                    poid = (obj.get("metadata") or {}).get("order_id") or data.get("id")
                    if not isinstance(poid, str) or _PROVIDER_ORDER_ID_RE.fullmatch(poid) is None:
                        poid = "order_" + hashlib.sha256(body).hexdigest()[:16]
                created = data.get("created")
                if not isinstance(created, int):
                    created = int.from_bytes(hashlib.sha256(body).digest()[:4], "big") % _MAX_TIME
                eid = data.get("id")
                if not isinstance(eid, str) or _EVENT_ID_RE.fullmatch(eid) is None:
                    eid = "evt_" + hashlib.sha256(body).hexdigest()[:16]
                return {
                    "event_id": eid,
                    "event_type": et,
                    "provider_order_id": poid,
                    "amount_paise": amt,
                    "currency": "INR",
                    "occurred_at": created,
                }
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError("Invalid webhook event.") from exc
        raise ProviderError("Invalid webhook event.")


_ADAPTERS: dict[str, _BaseAdapter] = {
    "razorpay": _RazorpayAdapter(),
    "stripe": _StripeAdapter(),
}


def _adapter(provider: str) -> _BaseAdapter:
    # provider is validated; return adapter
    return _ADAPTERS[provider]


def _validate_webhook_body(provider: str, body: bytes, now: int) -> dict:
    # Legacy wrapper: delegate to adapter normalize then validate fields strictly
    adapter = _adapter(provider)
    data = adapter.normalize(body)
    # Strict field checks (same as before, but now provider-agnostic)
    if type(body) is not bytes or not 1 <= len(body) <= _MAX_BODY:
        raise ProviderError("Invalid webhook event.")
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
    return data


def _verify_razorpay_signature(secret: bytes, body: bytes, signature: str) -> None:
    _ADAPTERS["razorpay"].verify(secret, body, signature, 0)


def _verify_stripe_signature(secret: bytes, body: bytes, signature: str, now: int) -> None:
    _ADAPTERS["stripe"].verify(secret, body, signature, now)


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
    """Sandbox provider orders — HMAC verified, idempotent, audited, no live network.

    Modular: per-provider adapters handle verification & normalization.
    Order creation = payment order creation. Initiation is implicit in `created`
    status; webhook `authorized` marks initiation verified. Never marks
    `received`/`settled` from client success — only verified webhook.
    """

    def __init__(self, auth: AuthService, state: StateStorage, config: ProviderConfig):
        self._auth = auth
        self._state = state
        self._config = config
        self._adapter = _adapter(config.provider)

    # ---- payment order creation (also initiation record) ----

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
            if conn.execute("SELECT 1 FROM payment_provider_orders WHERE provider_order_id=? AND owner_id=?", (provider_order_id, owner)).fetchone() is not None:
                provider_order_id = f"order_{secrets.token_hex(8)}"
            conn.execute(
                "INSERT INTO payment_provider_orders(id, owner_id, provider, mode, amount_paise, currency, status, provider_order_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, owner, self._config.provider, self._config.mode, amt, "INR", "created", provider_order_id, now, now),
            )
            _audit(conn, now, owner, "provider_order_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    # alias for initiation semantics
    def initiate_payment(self, token: str, amount_paise: int, *, operation_key: str) -> str:
        """Create provider order — initiation is order `created`, not `authorized`."""
        return self.create_order(token, amount_paise, operation_key=operation_key)

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

    def list_events(self, token: str, order_id: str) -> list[dict]:
        """Event log for an order — owner-isolated, ordered by received_at."""
        ident = _input(_record_id, order_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned(conn, owner, ident)
            rows = conn.execute(
                "SELECT event_id, event_type, amount_paise, received_at FROM payment_provider_events WHERE owner_id=? AND order_id=? ORDER BY received_at, event_id",
                (owner, ident),
            ).fetchall()
            return [dict(r) for r in rows]

    def handle_webhook(self, token: str, raw_body: bytes, signature: str, *, operation_key: str) -> dict:
        """Verify HMAC over raw bytes, validate body, update order status idempotently.

        Returns {"event_id": ..., "order_id": ..., "new_status": ...}
        Never trusts provider status without verification and amount binding.
        Sandbox and production secrets are kept separate via ProviderConfig mode.
        """
        key = _input(_key, operation_key)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            # Verify signature over raw bytes before parsing (per provider docs)
            self._adapter.verify(self._config.webhook_secret, raw_body, signature, now)
            data = _validate_webhook_body(self._config.provider, raw_body, now)
            body_hash = hashlib.sha256(raw_body).digest()
            digest = _request("provider_webhook", [data["event_id"], body_hash.hex()])
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                order_row = _owned(conn, owner, existing)
                if order_row["provider_order_id"] != data["provider_order_id"]:
                    raise StateStorageError("Stored operation result is inconsistent.")
                ev = conn.execute("SELECT order_id, raw_body_hash FROM payment_provider_events WHERE owner_id=? AND event_id=?", (owner, data["event_id"])).fetchone()
                if ev is None or ev["order_id"] != existing or not hmac.compare_digest(ev["raw_body_hash"], body_hash):
                    raise StateStorageError("Stored webhook event is inconsistent.")
                return {"event_id": data["event_id"], "order_id": existing, "new_status": order_row["status"]}
            order = conn.execute("SELECT id, provider, mode, amount_paise, status FROM payment_provider_orders WHERE provider_order_id=? AND owner_id=?", (data["provider_order_id"], owner)).fetchone()
            if order is None:
                raise AccessDenied("Access denied.")
            if order["provider"] != self._config.provider or order["mode"] != self._config.mode:
                raise AccessDenied("Access denied.")
            if order["amount_paise"] != data["amount_paise"]:
                raise ProviderError("Webhook amount mismatch.")
            # Card details are never stored; reject if body appears to contain them (defense in depth)
            if b"card" in raw_body.lower() and b"number" in raw_body.lower():
                # Heuristic: real card numbers should never appear in webhook; treat as invalid
                # But don't break sandbox minimal shape — only if extra fields contain card-like patterns
                try:
                    raw_str = raw_body.decode("utf-8", errors="ignore").lower()
                    if "card_number" in raw_str or "card[number]" in raw_str or re.search(r'"card[^"]*"\s*:\s*"\d{13,19}"', raw_str):
                        raise ProviderError("Invalid webhook event.")
                except ProviderError:
                    raise
                except Exception:
                    pass
            prior = conn.execute("SELECT order_id, raw_body_hash, event_type FROM payment_provider_events WHERE owner_id=? AND event_id=?", (owner, data["event_id"])).fetchone()
            if prior is not None:
                if prior["order_id"] != order["id"] or not hmac.compare_digest(prior["raw_body_hash"], body_hash) or prior["event_type"] != data["event_type"]:
                    raise ProviderError("Conflicting webhook event.")
                raise ProviderError("Webhook event already processed.")
            current_status = order["status"]
            expected = _EVENT_TO_STATUS[data["event_type"]]
            transition_event = f"provider_order_{expected}"
            if (current_status, transition_event) not in _TRANSITIONS:
                raise ProviderError("Transition is not allowed.")
            # Re-verify freshness before commit (replay protection)
            self._adapter.verify(self._config.webhook_secret, raw_body, signature, now)
            conn.execute("INSERT INTO payment_provider_events(owner_id, event_id, order_id, event_type, amount_paise, raw_body_hash, received_at) VALUES(?,?,?,?,?,?,?)",
                         (owner, data["event_id"], order["id"], data["event_type"], data["amount_paise"], body_hash, now))
            conn.execute("UPDATE payment_provider_orders SET status=?, updated_at=? WHERE id=? AND owner_id=?", (expected, now, order["id"], owner))
            _audit(conn, now, owner, transition_event, order["id"])
            _remember(conn, owner, key, digest, order["id"])
            return {"event_id": data["event_id"], "order_id": order["id"], "new_status": expected}

    def handle_refund(self, token: str, order_id: str, amount_paise: int, *, operation_key: str) -> str:
        """Initiate refund via webhook-like flow: create refund event for a captured/settled order.

        Sandbox only: records a `refunded` transition if allowed; does not call provider.
        """
        ident = _input(_record_id, order_id)
        amt = _input(_amount, amount_paise)
        key = _input(_key, operation_key)
        digest = _request("provider_refund", [ident, amt])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            row = _owned(conn, owner, ident)
            if row["provider"] != self._config.provider or row["mode"] != self._config.mode:
                raise AccessDenied("Access denied.")
            if row["status"] not in ("captured", "settled"):
                raise ProviderError("Refund is not allowed.")
            if amt > row["amount_paise"]:
                raise ProviderError("Refund amount exceeds order amount.")
            if row["mode"] != "sandbox":
                raise ProviderError("Refund only supported in sandbox mode.")
            # Create a synthetic refund event locally (would be webhook in production)
            event_id = f"re_{ident[:8]}_{secrets.token_hex(4)}"
            body_dict = {"event_id": event_id, "event_type": "refunded", "provider_order_id": row["provider_order_id"], "amount_paise": amt, "currency": "INR", "occurred_at": now}
            body = json.dumps(body_dict, separators=(",", ":")).encode()
            body_hash = hashlib.sha256(body).digest()
            # No external signature in this local refund initiation; we log as internal event
            conn.execute("INSERT INTO payment_provider_events(owner_id, event_id, order_id, event_type, amount_paise, raw_body_hash, received_at) VALUES(?,?,?,?,?,?,?)",
                         (owner, event_id, ident, "refunded", amt, body_hash, now))
            conn.execute("UPDATE payment_provider_orders SET status='refunded', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "provider_order_refunded", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get_settlement_status(self, token: str, order_id: str) -> dict:
        """Settlement status — returns provider-side settled flag, never inferred."""
        ident = _input(_record_id, order_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            events = conn.execute("SELECT event_type, received_at FROM payment_provider_events WHERE owner_id=? AND order_id=? ORDER BY received_at", (owner, ident)).fetchall()
            settled = any(e["event_type"] == "settled" for e in events)
            return {"order_id": ident, "provider": row["provider"], "mode": row["mode"], "status": row["status"], "settled": settled, "events": [dict(e) for e in events]}

    def reconcile(self, token: str, record_id: str) -> dict:
        """Local reconciliation: compare stored order status vs event replay, check amount binding.

        Does not fetch provider; verifies that local event log and audit are consistent
        and that no card details were stored.
        """
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = _owned(conn, owner, ident)
            events = conn.execute("SELECT event_id, event_type, amount_paise, raw_body_hash, received_at FROM payment_provider_events WHERE owner_id=? AND order_id=? ORDER BY received_at", (owner, ident)).fetchall()
            # Verify no card details in stored hashes (defense: raw bodies hashed, but ensure no card column exists)
            # Check schema has no card columns
            cols = [r[1] for r in conn.execute("PRAGMA table_info(payment_provider_orders)").fetchall()]
            card_cols = [c for c in cols if "card" in c.lower()]
            if card_cols:
                raise StateStorageError("Provider orders must not store card details.")
            cols2 = [r[1] for r in conn.execute("PRAGMA table_info(payment_provider_events)").fetchall()]
            if any("card" in c.lower() for c in cols2):
                raise StateStorageError("Provider events must not store card details.")
            # Amount consistency: all events must match order amount
            for e in events:
                if e["amount_paise"] != row["amount_paise"]:
                    raise StateStorageError("Reconciliation amount mismatch.")
            # Audit vs events consistency already checked in _owned; add event count check
            audit_events = conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id", (owner, _RESOURCE, ident)).fetchall()
            # Filter non-order events already handled; just report counts
            return {"order": dict(row), "event_count": len(events), "events": [dict(e) for e in events], "audit_count": len(audit_events), "mode": row["mode"], "provider": row["provider"], "reconciliation": "local_only", "card_storage": "none"}

