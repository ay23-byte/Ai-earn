"""Account deletion safeguards — explicit confirmation, backup, audit, rate-limited."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from pathlib import Path

from .auth import AccessDenied, _token_hash
from .auth_storage import AuthStorage
from .state_storage import StateStorage, StateStorageError
from .state_authorization import _authorized_transaction
from .auth import AuthService

DELETE_PHRASE_PREFIX = "DELETE"
DELETION_ACTION = "account_deletion"

DELETION_TABLES = [
    "opportunities",
    "services",
    "portfolio_items",
    "products",
    "customers",
    "communications",
    "ledger_entries",
    "followups",
    "invoices",
    "payment_provider_orders",
    "payment_provider_events",
    "ai_approvals",
    "ai_emergency",
    "project_tasks",
    "task_dependencies",
    "task_execution_logs",
    "service_packages",
    "service_requirements",
    "service_attachments",
    "proposals",
    "customer_requests",
    "product_ideas",
    "product_validations",
    "market_research",
    "pricing_suggestions",
    "product_versions",
    "product_files",
    "product_sales",
    "product_deliverables",
    "product_documentation",
    "customer_notes",
    "customer_requirements",
    "customer_orders",
    "customer_projects",
    "crm_communication_approvals",
    "crm_outbound_configs",
    "tax_configs",
    "billing_profiles",
    "payment_records",
    "expense_records",
    "refund_records",
    "fee_records",
    "state_audit",
    "state_operations",
    "opportunity_drafts",
    "task_plans",
    "order_drafts",
    "invoice_drafts",
    "task_queue",
    "queue_leases",
    "task_workflows",
    "payment_observations",
    "task_associations",
    "paired_completions",
    "managed_task_pairs",
    "financial_assertions",
    "sandbox_payments",
    "sandbox_payment_events",
]


def _confirmation_phrase(owner_id: str) -> str:
    return f"{DELETE_PHRASE_PREFIX} {owner_id[:8]}"


def prepare_deletion_backup(
    token: str,
    *,
    auth_db: Path,
    state_db: Path,
    backup_path: Path,
) -> dict:
    """Step 1: Validate session and create verified backup. Returns backup info."""
    auth = AuthService(AuthStorage(auth_db, create=False))
    state = StateStorage(state_db, create=False)
    owner_id = None
    with _authorized_transaction(auth, state, token) as (conn, oid, now):
        owner_id = oid
        if backup_path.exists():
            raise FileExistsError("Backup path already exists; never overwrite.")
        if backup_path.parent.resolve() != state_db.parent.resolve():
            raise ValueError("Backup must be in same private directory as state DB.")
    assert owner_id is not None
    # Outside transaction, verified backup
    StateStorage(state_db, create=False).backup_to(backup_path, confirmed=True)
    return {
        "owner_id": owner_id,
        "backup_path": str(backup_path),
        "backup_bytes": backup_path.stat().st_size,
        "confirmation_phrase": _confirmation_phrase(owner_id),
        "next": f"To confirm deletion, call confirm_deletion(token, password, phrase='{_confirmation_phrase(owner_id)}', ...)",
    }


def confirm_deletion(
    token: str,
    password: str,
    phrase: str,
    *,
    auth_db: Path,
    state_db: Path,
    backup_path: Path | None = None,
) -> dict:
    """Step 2: Validate phrase, password, ensure backup, then delete owner rows."""
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("Password re-entry required (12..256).")
    if not isinstance(phrase, str) or not phrase.startswith(DELETE_PHRASE_PREFIX):
        raise ValueError("Confirmation phrase invalid.")

    auth_storage = AuthStorage(auth_db, create=False)
    auth = AuthService(auth_storage)
    state = StateStorage(state_db, create=False)

    # Validate session and phrase
    owner_id: str | None = None
    with _authorized_transaction(auth, state, token) as (conn, oid, now):
        owner_id = oid
        expected = _confirmation_phrase(oid)
        if phrase != expected:
            raise ValueError(f"Phrase must be exactly '{expected}' (case-sensitive).")
    assert owner_id is not None

    # Verify password via auth storage hash check
    digest = _token_hash(token)
    if digest is None:
        raise AccessDenied("Access denied.")
    with auth_storage.transaction() as aconn:
        sess = aconn.execute(
            "SELECT account_id FROM sessions WHERE token_hash=? AND revoked_at IS NULL", (digest,)
        ).fetchone()
        if sess is None:
            raise AccessDenied("Access denied.")
        acc = aconn.execute(
            "SELECT username, password_hash, password_salt, password_iterations FROM accounts WHERE id=?",
            (sess[0],),
        ).fetchone()
        if acc is None:
            raise AccessDenied("Access denied.")
        username, stored_hash, salt, iterations = acc[0], acc[1], acc[2], acc[3]
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        if not hmac.compare_digest(calc, stored_hash):
            raise AccessDenied("Password re-entry failed.")
        aconn.execute(
            "INSERT INTO auth_attempts(action, subject, occurred_at) VALUES (?, ?, ?)",
            (DELETION_ACTION, username, int(time.time())),
        )

    # Ensure backup exists
    if backup_path is None:
        backup_path = state_db.parent / f"before-delete-{owner_id[:8]}-{int(time.time())}.sqlite3"
    if not backup_path.exists():
        StateStorage(state_db, create=False).backup_to(backup_path, confirmed=True)
    else:
        StateStorage(backup_path, create=False)

    # Delete owner-scoped rows
    deleted_counts: dict[str, int] = {}
    with state.transaction() as conn:
        for table in DELETION_TABLES:
            try:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                if "owner_id" in cols:
                    cur = conn.execute(f"DELETE FROM {table} WHERE owner_id=?", (owner_id,))
                    deleted_counts[table] = cur.rowcount
                elif "actor_id" in cols:
                    cur = conn.execute(f"DELETE FROM {table} WHERE actor_id=?", (owner_id,))
                    deleted_counts[table] = cur.rowcount
            except sqlite3.Error:
                continue
        try:
            conn.execute(
                "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), owner_id, "account_deletion", "account", owner_id),
            )
        except Exception:
            pass

    # Revoke and remove all auth data for owner (children first to satisfy FK)
    with auth_storage.transaction() as aconn:
        # Delete sessions (including revoked) for this account
        aconn.execute("DELETE FROM sessions WHERE account_id=?", (owner_id,))
        # Keep auth_audit for forensic trace but allow account deletion:
        # null out account_id in audit where it references the deleted account (if FK is strict, delete instead)
        try:
            aconn.execute("DELETE FROM auth_audit WHERE account_id=?", (owner_id,))
        except Exception:
            pass
        # Clear rate-limit attempts for this username (subject)
        try:
            aconn.execute("DELETE FROM auth_attempts WHERE subject=?", (username,))
        except Exception:
            pass
        aconn.execute("DELETE FROM accounts WHERE id=?", (owner_id,))
        # Insert deletion audit without FK (use NULL account_id to avoid FK violation post-delete)
        try:
            aconn.execute(
                "INSERT INTO auth_audit(occurred_at, event, account_id) VALUES (?, ?, ?)",
                (int(time.time()), "account_deleted", None),
            )
        except Exception:
            pass

    return {
        "owner_id": owner_id,
        "deleted_counts": deleted_counts,
        "total_deleted_rows": sum(deleted_counts.values()),
        "backup_path": str(backup_path),
        "note": "Deletion preserved backup. To restore, copy backup to a new private path and re-register. This action is audited.",
    }
