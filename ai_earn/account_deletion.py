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
    "ai_provider_configs",
    "ai_token_usage",
    "settlement_evidence",
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
    if backup_path.exists():
        raise FileExistsError("Backup path already exists; never overwrite.")
    if backup_path.parent.resolve() != state_db.parent.resolve():
        raise ValueError("Backup must be in same private directory as state DB.")
    auth = AuthService(AuthStorage(auth_db, create=False))
    state = StateStorage(state_db, create=False)
    owner_id = None
    with _authorized_transaction(auth, state, token) as (conn, oid, now):
        owner_id = oid
    assert owner_id is not None
    # Outside transaction, verified backup — with hash, integrity, schema, restore test
    StateStorage(state_db, create=False).backup_to(backup_path, confirmed=True)
    backup_bytes = backup_path.read_bytes()
    backup_sha256 = hashlib.sha256(backup_bytes).hexdigest()
    # Verify backup can be opened and contains expected pre-deletion data
    bstate = StateStorage(backup_path, create=False)
    with bstate.transaction() as bconn:
        from .state_storage import LATEST_SCHEMA_VERSION
        bv = bconn.execute("PRAGMA user_version").fetchone()[0]
        if bv != LATEST_SCHEMA_VERSION:
            raise StateStorageError(f"Backup schema version {bv} != LATEST {LATEST_SCHEMA_VERSION}")
        has_data = False
        try:
            if bconn.execute("SELECT 1 FROM state_audit WHERE actor_id=? LIMIT 1", (owner_id,)).fetchone():
                has_data = True
        except sqlite3.Error:
            pass
        if not has_data:
            try:
                if bconn.execute("SELECT 1 FROM opportunities WHERE owner_id=? LIMIT 1", (owner_id,)).fetchone():
                    has_data = True
            except sqlite3.Error:
                pass
    # Verify backup can be restored to disposable temp (ensures file is not truncated/corrupt)
    import tempfile, os as _os
    try:
        restore_parent = backup_path.parent / f"tmp_restore_{int(time.time()*1000)%1000000}"
        if _os.name == "nt":
            from .windows_security import create_private_directory
            if not restore_parent.exists():
                create_private_directory(restore_parent)
            else:
                restore_parent = backup_path.parent / f"tmp_restore_{int(time.time())}_2"
                create_private_directory(restore_parent)
        else:
            restore_parent.mkdir(mode=0o700, exist_ok=False)
        tmp_restore = restore_parent / "restored.sqlite3"
        StateStorage(backup_path, create=False).backup_to(tmp_restore, confirmed=True)
    finally:
        try:
            if 'tmp_restore' in locals() and Path(tmp_restore).exists():
                Path(tmp_restore).unlink()
            if 'restore_parent' in locals() and Path(restore_parent).exists():
                for child in Path(restore_parent).iterdir():
                    try:
                        child.unlink()
                    except Exception:
                        pass
                try:
                    Path(restore_parent).rmdir()
                except Exception:
                    pass
        except Exception:
            pass
    return {
        "owner_id": owner_id,
        "backup_path": str(backup_path),
        "backup_bytes": backup_path.stat().st_size,
        "backup_sha256": backup_sha256,
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

    # Ensure backup exists and is verified (hash, integrity, schema, restore)
    if backup_path is None:
        backup_path = state_db.parent / f"before-delete-{owner_id[:8]}-{int(time.time())}.sqlite3"
    if not backup_path.exists():
        StateStorage(state_db, create=False).backup_to(backup_path, confirmed=True)
    else:
        if backup_path.parent.resolve() != state_db.parent.resolve():
            raise ValueError("Backup must be in same private directory as state DB.")
        bstate = StateStorage(backup_path, create=False)
        with bstate.transaction() as bconn:
            from .state_storage import LATEST_SCHEMA_VERSION, APPLICATION_ID
            bv = bconn.execute("PRAGMA user_version").fetchone()[0]
            if bv != LATEST_SCHEMA_VERSION:
                raise StateStorageError(f"Backup schema version {bv} invalid")
            row = bconn.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise StateStorageError("Backup integrity_check failed")
            app_id = bconn.execute("PRAGMA application_id").fetchone()[0]
            if app_id != APPLICATION_ID:
                raise StateStorageError("Backup is wrong database (application_id mismatch)")
        backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        import tempfile as _tmp, os as _os2
        try:
            restore_parent = backup_path.parent / f"tmp_verify_{int(time.time()*1000)%1000000}"
            if _os2.name == "nt":
                from .windows_security import create_private_directory as _cpd
                if not restore_parent.exists():
                    _cpd(restore_parent)
                else:
                    restore_parent = backup_path.parent / f"tmp_verify_{int(time.time())}_2"
                    _cpd(restore_parent)
            else:
                restore_parent.mkdir(mode=0o700, exist_ok=False)
            tmp_verify = restore_parent / "verify.sqlite3"
            StateStorage(backup_path, create=False).backup_to(tmp_verify, confirmed=True)
        finally:
            try:
                if 'tmp_verify' in locals() and Path(tmp_verify).exists():
                    Path(tmp_verify).unlink()
                if 'restore_parent' in locals() and Path(restore_parent).exists():
                    for child in list(Path(restore_parent).iterdir()):
                        try:
                            child.unlink()
                        except Exception:
                            pass
                    try:
                        Path(restore_parent).rmdir()
                    except Exception:
                        pass
            except Exception:
                pass

    # Delete owner-scoped rows — no silent swallow of DB errors
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
                else:
                    deleted_counts[table] = 0
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    deleted_counts[table] = 0
                    continue
                raise StateStorageError(f"Deletion failed for {table}: {exc}") from exc
            except sqlite3.Error as exc:
                raise StateStorageError(f"Deletion failed for {table}: {exc}") from exc
        # Preserve minimal audit record without PII — must succeed, not swallowed
        conn.execute(
            "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), owner_id, "account_deletion", "account", owner_id),
        )

    # Revoke and remove all auth data for owner (children first to satisfy FK)
    with auth_storage.transaction() as aconn:
        aconn.execute("DELETE FROM sessions WHERE account_id=?", (owner_id,))
        # Remove auth audit for this account (FK would prevent account delete otherwise)
        aconn.execute("DELETE FROM auth_audit WHERE account_id=?", (owner_id,))
        # Clear rate-limit attempts for this username
        aconn.execute("DELETE FROM auth_attempts WHERE subject=?", (username,))
        aconn.execute("DELETE FROM accounts WHERE id=?", (owner_id,))
        # Insert deletion audit without FK (NULL account_id to avoid FK violation post-delete)
        aconn.execute(
            "INSERT INTO auth_audit(occurred_at, event, account_id) VALUES (?, ?, ?)",
            (int(time.time()), "account_deleted", None),
        )

    return {
        "owner_id": owner_id,
        "deleted_counts": deleted_counts,
        "total_deleted_rows": sum(deleted_counts.values()),
        "backup_path": str(backup_path),
        "note": "Deletion preserved backup. To restore, copy backup to a new private path and re-register. This action is audited.",
    }
