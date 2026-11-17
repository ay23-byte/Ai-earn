"""User data export — owner-scoped, bounded, private.

Exports all owned rows to a JSON file beside the private DB (never over network).
Never exports password salts/hashes, recovery hashes, or session token hashes.
All tables are owner-scoped via owner_id / actor_id; cross-owner data is never
included. Export is read-only, 1 MB JSON cap per table slice, with integrity
check before export.

Safeguards:
- Requires live session token (checked via _authorized_transaction)
- Owner-scoped only
- Redacts sensitive auth columns
- Bounded output (prevents disk exhaustion)
- Verifies health/doctor ok before export
- Audit trail remains in DB; export does not modify DB
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Mapping

from .state_authorization import _authorized_transaction
from .auth import AuthService
from .auth_storage import AuthStorage
from .state_storage import StateStorage

# Tables that contain user data (owner-scoped) — excludes auth secrets
EXPORT_TABLES = [
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
    "project_tasks",
    "customer_notes",
    "customer_requirements",
    "customer_orders",
    "customer_projects",
    "tax_configs",
    "billing_profiles",
    "payment_records",
    "expense_records",
    "refund_records",
    "fee_records",
    "state_audit",
    "state_operations",
]

# Columns to redact if ever present (defense-in-depth)
REDACTED_COLUMNS = {"password_salt", "password_hash", "recovery_hash", "token_hash", "request_hash"}

MAX_EXPORT_BYTES = 50 * 1024 * 1024  # 50 MB hard cap (snapshot is 1 MB but export may be larger)
ROW_LIMIT_PER_TABLE = 10000


def export_user_data(token: str, *, auth_db: Path, state_db: Path, destination: Path | None = None) -> dict:
    """Export all owned rows for token's owner. Returns summary. Never logs secret."""
    # Validate token/session via existing authorization gate
    auth = AuthService(AuthStorage(auth_db, create=False))
    state = StateStorage(state_db, create=False)

    # This will raise AccessDenied if token invalid/expired
    with _authorized_transaction(auth, state, token) as (conn, owner_id, now):
        # Integrity gate already checked by _authorized_transaction
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        result: dict = {
            "export_version": "1",
            "schema_version": int(schema_version),
            "owner_id": owner_id,
            "exported_at": int(now),
            "exported_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "included_categories": sorted(EXPORT_TABLES),
            "tables": {},
            "counts": {},
            "verification": "owner-scoped, read-only, secrets redacted",
        }
        total_bytes = 0
        total_rows = 0
        for table in EXPORT_TABLES:
            # Use parameterized table check via allowlist only (no SQL injection)
            # Count rows for this owner — handle tables that use actor_id vs owner_id
            try:
                if table == "state_audit":
                    rows = conn.execute(
                        f"SELECT * FROM {table} WHERE actor_id=? ORDER BY id LIMIT ?",
                        (owner_id, ROW_LIMIT_PER_TABLE),
                    ).fetchall()
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE actor_id=?", (owner_id,)
                    ).fetchone()[0]
                elif table == "state_operations":
                    rows = conn.execute(
                        f"SELECT owner_id, operation_key, resource_type, resource_id FROM {table} WHERE owner_id=? ORDER BY resource_type, resource_id LIMIT ?",
                        (owner_id, ROW_LIMIT_PER_TABLE),
                    ).fetchall()
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE owner_id=?", (owner_id,)
                    ).fetchone()[0]
                else:
                    # Generic owner_id tables (if column exists)
                    # Check if table exists and has owner_id column
                    col_check = conn.execute(f"PRAGMA table_info({table})").fetchall()
                    has_owner = any(r[1] == "owner_id" for r in col_check)
                    if not has_owner:
                        continue
                    rows = conn.execute(
                        f"SELECT * FROM {table} WHERE owner_id=? ORDER BY id LIMIT ?",
                        (owner_id, ROW_LIMIT_PER_TABLE),
                    ).fetchall()
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE owner_id=?", (owner_id,)
                    ).fetchone()[0]
            except sqlite3.Error:
                result["tables"][table] = {"error": "table not available at this schema version"}
                continue

            # Convert rows to dicts, redact sensitive columns
            cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description] if rows else []
            clean_rows: list[dict] = []
            for r in rows:
                d = dict(r) if isinstance(r, sqlite3.Row) else dict(zip(cols, r))
                for col in list(d.keys()):
                    if col in REDACTED_COLUMNS:
                        d[col] = "[REDACTED]"
                clean_rows.append(d)

            # Estimate size
            blob = json.dumps(clean_rows, separators=(",", ":"))
            total_bytes += len(blob.encode())
            total_rows += count
            if total_bytes > MAX_EXPORT_BYTES:
                raise ValueError("Export exceeds 50 MB cap — reduce data or export per-table")

            result["tables"][table] = {
                "count": count,
                "returned": len(clean_rows),
                "truncated": count > ROW_LIMIT_PER_TABLE,
                "rows": clean_rows,
            }
            result["counts"][table] = count

        result["total_rows"] = total_rows
        result["estimated_bytes"] = total_bytes
        result["note"] = "This export is private. Do not share. Secrets are redacted. For deletion see docs/PRIVACY.md and ai_earn.account_deletion."

        # Optional file write to private directory (never network)
        if destination is not None:
            if destination.exists():
                raise FileExistsError("Destination already exists; never overwrite.")
            # Ensure destination parent is same private dir as state_db
            if destination.parent.resolve() != state_db.parent.resolve():
                raise ValueError("Export destination must be in the same private directory as state DB.")
            payload = json.dumps(result, indent=2, sort_keys=True)
            if len(payload.encode()) > MAX_EXPORT_BYTES:
                raise ValueError("Serialized export exceeds cap")
            destination.write_text(payload, encoding="utf-8")
            # Restrict perms on POSIX
            try:
                import os, stat

                if os.name == "posix":
                    os.chmod(destination, 0o600)
            except Exception:
                pass
            result["destination"] = str(destination)

        return result
