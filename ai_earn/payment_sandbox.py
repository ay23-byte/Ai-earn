"""Synthetic in-memory payment-event policy; never actual financial evidence.

This test helper is not an HTTP endpoint, provider adapter, durable journal,
ledger, or production payment verifier. It neither receives nor sends money.
All events, states and acknowledgements are explicitly synthetic. Configuration
must come from trusted test composition, not an untrusted event sender.
"""
import hashlib
import hmac
import json
import re
import threading


_ID = re.compile(r"[0-9a-f]{32}\Z")
_EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SIGNATURE = re.compile(r"[0-9a-f]{64}\Z")
_KEYS = frozenset({"event_id", "event_type", "sequence", "payment_id", "amount_paise", "currency", "occurred_at"})
_MAX_TIME = 4_102_444_800


class SandboxPaymentError(Exception):
    """Invalid synthetic event, authentication, sequence or test configuration."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SandboxPaymentError("Invalid synthetic event.")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise SandboxPaymentError("Invalid synthetic event.")


class SandboxPaymentEvents:
    """One synthetic test payment, in memory only; state is lost on restart.

    HMAC here authenticates bytes using a caller-supplied *test* key. It does
    not authenticate a real provider or establish a payment, settlement,
    refund, bank balance or revenue. Never use with live or customer data.
    """

    def __init__(self, key: bytes, *, payment_id: str, amount_paise: int):
        if (type(key) is not bytes or not 32 <= len(key) <= 128
                or not isinstance(payment_id, str) or _ID.fullmatch(payment_id) is None
                or type(amount_paise) is not int or not 0 <= amount_paise <= 1_000_000_000_000):
            raise SandboxPaymentError("Invalid synthetic test configuration.")
        self._key = key
        self._payment_id = payment_id
        self._amount_paise = amount_paise
        self._lock = threading.Lock()
        self._state = "sandbox_awaiting"
        self._sequence = 0
        self._seen = {}

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def accept(self, body: bytes, signature: str, *, now: int) -> dict:
        """Authenticate and apply one synthetic event; no persistent state.

        Exact same-byte retries return their historical acknowledgement after
        signature/freshness validation. Event-ID conflicts and sequence gaps
        fail closed. Neither a state nor an acknowledgement is financial proof.
        """
        if (type(body) is not bytes or not 1 <= len(body) <= 4096
                or not isinstance(signature, str) or _SIGNATURE.fullmatch(signature) is None
                or type(now) is not int or not 0 <= now <= _MAX_TIME):
            raise SandboxPaymentError("Invalid synthetic event.")
        actual = hmac.new(self._key, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual, signature):
            raise SandboxPaymentError("Invalid synthetic event.")
        try:
            data = json.loads(body.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise SandboxPaymentError("Invalid synthetic event.") from exc
        if (type(data) is not dict or frozenset(data) != _KEYS
                or not isinstance(data["event_id"], str) or _EVENT_ID.fullmatch(data["event_id"]) is None
                or type(data["sequence"]) is not int or not 1 <= data["sequence"] <= 3
                or type(data["occurred_at"]) is not int or not 0 <= data["occurred_at"] <= _MAX_TIME
                or abs(data["occurred_at"] - now) > 300
                or data["payment_id"] != self._payment_id
                or type(data["amount_paise"]) is not int or data["amount_paise"] != self._amount_paise
                or data["currency"] != "INR"
                or data["event_type"] not in ("confirmation", "settlement", "failure", "refund")):
            raise SandboxPaymentError("Invalid synthetic event.")
        digest = hashlib.sha256(body).digest()
        with self._lock:
            prior = self._seen.get(data["event_id"])
            if prior is not None:
                if not hmac.compare_digest(prior[0], digest):
                    raise SandboxPaymentError("Conflicting synthetic event.")
                return dict(prior[1])
            transitions = {
                ("sandbox_awaiting", "confirmation"): "sandbox_confirmed",
                ("sandbox_awaiting", "failure"): "sandbox_failed",
                ("sandbox_confirmed", "settlement"): "sandbox_settled",
                ("sandbox_settled", "refund"): "sandbox_refunded",
            }
            stage = transitions.get((self._state, data["event_type"]))
            if stage is None or data["sequence"] != self._sequence + 1:
                raise SandboxPaymentError("Synthetic transition is not allowed.")
            result = {"event_id": data["event_id"], "acknowledged_stage": stage, "scope": "synthetic_only"}
            self._state = stage
            self._sequence = data["sequence"]
            self._seen[data["event_id"]] = (digest, result)
            return dict(result)
