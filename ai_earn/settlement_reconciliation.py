"""Verified settlement via local bank-statement CSV reconciliation (P2-1).

Sandbox/HMAC settlement is never verification. A payment becomes ``verified``
only when (1) a locally imported bank-statement CSV row matches its
provider reference and exact amount, and (2) a human explicitly confirms with
``confirmed=True``. No bank connection is made; the CSV is operator-supplied
evidence, and this module makes no external authenticity claim.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import secrets
import sqlite3

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest, _key, _record_id
from .state_authorization import _authorized_transaction
from .state_storage import LATEST_SCHEMA_VERSION, StateStorage, StateStorageError

_RESOURCE = "settlement_evidence"
_MAX_CSV_BYTES = 1_000_000
_MAX_ROWS = 10_000


class SettlementError(Exception):
    """Invalid input, unmatched evidence, or a disallowed transition."""


def _input(validator, value):
    try:
        return validator(value)
    except DraftError as exc:
        raise SettlementError("Invalid settlement input.") from exc


def _version(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if not 29 <= version <= LATEST_SCHEMA_VERSION:
        raise StateStorageError(
            f"State schema version 29 or newer (up to {LATEST_SCHEMA_VERSION}) is required for settlement reconciliation."
        )


def _retry(conn, owner, key, digest):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise SettlementError("Operation key was already used for a different request.")
    return row["resource_id"]


def _audit(conn, now, owner, event, ident):
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, event, _RESOURCE, ident),
    )


def _remember(conn, owner, key, digest, ident):
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, _RESOURCE, ident),
    )


def _parse_statement(csv_text: str) -> list[tuple[str, int]]:
    if not isinstance(csv_text, str) or not csv_text or len(csv_text.encode("utf-8")) > _MAX_CSV_BYTES:
        raise SettlementError("Invalid statement CSV.")
    try:
        rows = list(csv.DictReader(io.StringIO(csv_text)))
    except csv.Error as exc:
        raise SettlementError("Invalid statement CSV.") from exc
    if len(rows) > _MAX_ROWS:
        raise SettlementError("Statement CSV exceeds the row limit.")
    parsed: list[tuple[str, int]] = []
    for row in rows:
        if row is None or set(row) != {"provider_ref", "amount_paise"}:
            raise SettlementError("Statement CSV must have provider_ref,amount_paise columns.")
        ref, amount = row["provider_ref"], row["amount_paise"]
        if not isinstance(ref, str) or not 1 <= len(ref) <= 128 or any(ord(c) < 32 or ord(c) == 127 for c in ref):
            raise SettlementError("Invalid statement reference.")
        try:
            amount_value = int(amount)
        except (TypeError, ValueError) as exc:
            raise SettlementError("Invalid statement amount.") from exc
        if str(amount_value) != str(amount).strip() or not 0 <= amount_value <= 1_000_000_000_000:
            raise SettlementError("Invalid statement amount.")
        parsed.append((ref, amount_value))
    return parsed


class SettlementReconciliation:
    """Match settled payments to statement rows; verify only on human confirm."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def reconcile_statement(self, token: str, csv_text: str, *, operation_key: str) -> dict:
        """Record match evidence for settled, unverified payments. Never verifies."""
        key = _input(_key, operation_key)
        rows = _parse_statement(csv_text)
        digest = _digest("settlement_reconcile", hashlib.sha256(csv_text.encode("utf-8")).hexdigest())
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return {"matched": 0, "unmatched": 0, "replayed": True}
            matched = unmatched = 0
            for ref, amount in rows:
                payment = conn.execute(
                    "SELECT id, amount_paise, status, verified FROM payment_records WHERE owner_id=? AND provider_ref=?",
                    (owner, ref),
                ).fetchone()
                if payment is None or payment["status"] != "settled" or payment["amount_paise"] != amount:
                    unmatched += 1
                    continue
                evidence = conn.execute(
                    "SELECT id FROM settlement_evidence WHERE payment_id=? AND owner_id=?",
                    (payment["id"], owner),
                ).fetchone()
                if evidence is None:
                    ident = secrets.token_hex(16)
                    conn.execute(
                        "INSERT INTO settlement_evidence(id, owner_id, payment_id, statement_ref, amount_paise, matched, confirmed, created_at, updated_at) VALUES(?,?,?,?,?,1,0,?,?)",
                        (ident, owner, payment["id"], ref, amount, now, now),
                    )
                    _audit(conn, now, owner, "settlement_matched", ident)
                matched += 1
            _remember(conn, owner, key, digest, owner)
            return {"matched": matched, "unmatched": unmatched, "replayed": False}

    def confirm_verified(self, token: str, payment_id: str, *, confirmed: bool, operation_key: str) -> str:
        """Human sign-off: mark a matched settled payment verified. Idempotent."""
        pid = _input(_record_id, payment_id)
        key = _input(_key, operation_key)
        if confirmed is not True:
            raise SettlementError("Explicit human confirmation is required to verify a settlement.")
        digest = _digest("settlement_confirm_verified", pid)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return pid
            payment = conn.execute(
                "SELECT status, verified FROM payment_records WHERE id=? AND owner_id=?", (pid, owner)
            ).fetchone()
            if payment is None:
                raise AccessDenied("Access denied.")
            evidence = conn.execute(
                "SELECT id, matched FROM settlement_evidence WHERE payment_id=? AND owner_id=?", (pid, owner)
            ).fetchone()
            if evidence is None or evidence["matched"] != 1:
                raise SettlementError("No matching statement evidence; settlement cannot be verified.")
            if payment["status"] != "settled":
                raise SettlementError("Only settled payments can be verified.")
            conn.execute("UPDATE payment_records SET verified=1, updated_at=? WHERE id=? AND owner_id=?", (now, pid, owner))
            conn.execute("UPDATE settlement_evidence SET confirmed=1, updated_at=? WHERE id=?", (now, evidence["id"]))
            _audit(conn, now, owner, "settlement_verified", evidence["id"])
            _remember(conn, owner, key, digest, pid)
            return pid

    def evidence(self, token: str, payment_id: str) -> dict | None:
        pid = _input(_record_id, payment_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            if conn.execute("SELECT 1 FROM payment_records WHERE id=? AND owner_id=?", (pid, owner)).fetchone() is None:
                raise AccessDenied("Access denied.")
            row = conn.execute(
                "SELECT payment_id, statement_ref, amount_paise, matched, confirmed FROM settlement_evidence WHERE payment_id=? AND owner_id=?",
                (pid, owner),
            ).fetchone()
            return dict(row) if row is not None else None
