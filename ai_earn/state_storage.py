"""Internal Phase 3 SQLite foundation; no business API or authorization.

This database is distinct from the authentication database. Callers must not
use raw storage as a substitute for session-bound authorization. This module
contains experimental drafts and task plans, but no customer or payment data.
"""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator

from .auth_storage import AuthStorage, StorageError, _windows_auth_verified


APPLICATION_ID = 0x41494533  # AIE3, distinct from the auth-only database.
SCHEMA_VERSION = 1  # New files still bootstrap as version 1; upgrades are explicit.
LATEST_SCHEMA_VERSION = 28
SUPPORTED_STATE_VERSIONS = frozenset(range(1, LATEST_SCHEMA_VERSION + 1))


from .migrations import v02 as _migration_v2
from .migrations.v02 import _V2_SCHEMA
from .migrations import v03 as _migration_v3
from .migrations.v03 import _V3_SCHEMA
from .migrations import v04 as _migration_v4
from .migrations.v04 import _V4_SCHEMA
from .migrations import v05 as _migration_v5
from .migrations.v05 import _V5_SCHEMA
from .migrations import v06 as _migration_v6
from .migrations.v06 import _V6_INDEX, _V6_SCHEMA
from .migrations import v07 as _migration_v7
from .migrations.v07 import _V7_SCHEMA
from .migrations import v08 as _migration_v8
from .migrations.v08 import _V8_SCHEMA
from .migrations import v09 as _migration_v9
from .migrations.v09 import _V9_INDEX, _V9_SCHEMA
from .migrations import v10 as _migration_v10
from .migrations.v10 import _V10_INDEX, _V10_SCHEMA
from .migrations import v11 as _migration_v11
from .migrations.v11 import _V11_INDEX, _V11_SCHEMA
from .migrations import v12 as _migration_v12
from .migrations.v12 import _V12_SCHEMA
from .migrations import v13 as _migration_v13
from .migrations.v13 import _V13_SCHEMA
from .migrations import v14 as _migration_v14
from .migrations.v14 import _V14_SCHEMA
from .migrations import v15 as _migration_v15
from .migrations.v15 import _V15_SCHEMA
from .migrations import v16 as _migration_v16
from .migrations.v16 import _V16_INDEX, _V16_SCHEMA
from .migrations import v17 as _migration_v17
from .migrations.v17 import _V17_INDEX, _V17_SCHEMA
from .migrations import v18 as _migration_v18
from .migrations.v18 import _V18_INDEX, _V18_LEDGER_INDEX, _V18_SCHEMA
from .migrations import v19 as _migration_v19
from .migrations.v19 import _V19_CUSTOMER_INDEX, _V19_INDEX, _V19_SCHEMA
from .migrations import v20 as _migration_v20
from .migrations.v20 import _V20_INVOICE_INDEX, _V20_INVOICE_NUMBER_INDEX, _V20_SCHEMA
from .migrations import v21 as _migration_v21
from .migrations.v21 import (
    _V21_PROVIDER_ORDERS_ID_OWNER_IDX,
    _V21_PROVIDER_ORDERS_PROVIDER_IDX,
    _V21_PROVIDER_EVENTS_ID_IDX,
    _V21_SCHEMA,
)
from .migrations import v22 as _migration_v22
from .migrations.v22 import _V22_APPROVAL_INDEX, _V22_SCHEMA
from .migrations import v23 as _migration_v23
from .migrations.v23 import (
    _V23_PROJECT_TASKS_INDEX,
    _V23_DEPENDS_INDEX,
    _V23_LOGS_INDEX,
    _V23_SCHEMA,
)
from .migrations import v24 as _migration_v24
from .migrations.v24 import (
    _V24_LEASE_COL,
    _V24_UNTIL_COL,
    _V24_ATTEMPTS_COL,
    _V24_MAX_ATTEMPTS_COL,
    _V24_BACKOFF_COL,
    _V24_AVAILABLE_COL,
    _V24_SCHEMA,
)
from .migrations import v25 as _migration_v25
from .migrations.v25 import (
    _V25_PACKAGE_INDEX,
    _V25_REQUIREMENT_INDEX,
    _V25_ATTACHMENT_INDEX,
    _V25_PROPOSAL_INDEX,
    _V25_REQUEST_INDEX,
    _V25_SCHEMA,
)
from .migrations import v26 as _migration_v26
from .migrations.v26 import (
    _V26_IDEA_INDEX,
    _V26_VALIDATION_INDEX,
    _V26_RESEARCH_INDEX,
    _V26_PRICING_INDEX,
    _V26_VERSION_INDEX,
    _V26_FILE_INDEX,
    _V26_SALES_INDEX,
    _V26_DELIVERABLE_INDEX,
    _V26_DOCS_INDEX,
    _V26_SCHEMA,
)
from .migrations import v27 as _migration_v27
from .migrations.v27 import (
    _V27_NOTE_INDEX,
    _V27_REQUIREMENT_INDEX,
    _V27_ORDER_INDEX,
    _V27_ORDER_CUSTOMER_INDEX,
    _V27_PROJECT_CUSTOMER_INDEX,
    _V27_PROJECT_TASK_INDEX,
    _V27_COMM_APPROVAL_INDEX,
    _V27_COMM_APPROVAL_COMM_INDEX,
    _V27_OUTBOUND_INDEX,
    _V27_SCHEMA,
)
from .migrations import v28 as _migration_v28
from .migrations.v28 import (
    _V28_TAX_INDEX,
    _V28_BILLING_ID_INDEX,
    _V28_BILLING_CUSTOMER_INDEX,
    _V28_PAYMENT_ID_INDEX,
    _V28_PAYMENT_INVOICE_INDEX,
    _V28_PAYMENT_STATUS_INDEX,
    _V28_EXPENSE_ID_INDEX,
    _V28_REFUND_ID_INDEX,
    _V28_REFUND_PAYMENT_INDEX,
    _V28_FEE_ID_INDEX,
    _V28_FEE_PAYMENT_INDEX,
    _V28_SCHEMA,
)

_apply_v2 = _migration_v2.apply
_apply_v3 = _migration_v3.apply
_apply_v4 = _migration_v4.apply
_apply_v5 = _migration_v5.apply
_apply_v6 = _migration_v6.apply
_apply_v7 = _migration_v7.apply
_apply_v8 = _migration_v8.apply
_apply_v9 = _migration_v9.apply
_apply_v10 = _migration_v10.apply
_apply_v11 = _migration_v11.apply
_apply_v12 = _migration_v12.apply
_apply_v13 = _migration_v13.apply
_apply_v14 = _migration_v14.apply
_apply_v15 = _migration_v15.apply
_apply_v16 = _migration_v16.apply
_apply_v17 = _migration_v17.apply
_apply_v18 = _migration_v18.apply
_apply_v19 = _migration_v19.apply
_apply_v20 = _migration_v20.apply
_apply_v21 = _migration_v21.apply
_apply_v22 = _migration_v22.apply
_apply_v23 = _migration_v23.apply
_apply_v24 = _migration_v24.apply
_apply_v25 = _migration_v25.apply
_apply_v26 = _migration_v26.apply
_apply_v27 = _migration_v27.apply
_apply_v28 = _migration_v28.apply


class StateStorageError(Exception):
    """The state database is missing, unsafe, incompatible or unavailable."""


class _PrivateFiles(AuthStorage):
    """Reuse Phase 2 file/transaction checks, without its auth schema setup."""

    def __init__(self, path: Path):
        if os.name != "posix" and not (os.name == "nt" and _windows_auth_verified()):
            raise StateStorageError("State storage is disabled on this platform.")
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise StateStorageError("State database path must be absolute.")
        try:
            self._check_parent()
        except StorageError as exc:
            raise StateStorageError("State database directory is not private or unavailable.") from exc

    def create_exclusive(self) -> None:
        """Create a new private file only; never replace an existing path."""
        try:
            self._check_parent()
            # SQLite may otherwise reuse or remove stale sidecars belonging to
            # another file. Check before creating the main file; trusted
            # ancestors and OS account are still required to avoid races.
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(str(self.path) + suffix)
                try:
                    sidecar.lstat()
                except FileNotFoundError:
                    continue
                raise StateStorageError("Existing SQLite sidecars require investigation; no file was created.")
            if os.name == "nt":
                # Use the Win32 creator so the database owner and DACL are
                # established explicitly. os.open(mode=0o600) does not set
                # Windows ownership and can inherit an elevated runner owner.
                from .windows_security import create_private_file
                create_private_file(self.path)
            else:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(self.path, flags, 0o600)
                os.close(descriptor)
            self._ensure_file()
        except (OSError, StorageError) as exc:
            # A file created before a later error is left for inspection.
            raise StateStorageError("State file could not be created safely; existing files are never overwritten.") from exc
























































def _validate_schema(conn: sqlite3.Connection) -> int:
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if application_id != APPLICATION_ID or version not in SUPPORTED_STATE_VERSIONS:
        raise StateStorageError("Unsupported or uninitialized state database; no migration was performed.")
    expected_columns = {
        "state_meta": [("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)],
        "state_migrations": [("version", "INTEGER", 0, 1), ("applied_at", "INTEGER", 1, 0)],
    }
    if version >= 2:
        expected_columns.update({
            "state_audit": [("id", "INTEGER", 0, 1), ("occurred_at", "INTEGER", 1, 0),
                            ("actor_id", "TEXT", 1, 0), ("event", "TEXT", 1, 0),
                            ("resource_type", "TEXT", 1, 0), ("resource_id", "TEXT", 1, 0)],
            "state_operations": [("owner_id", "TEXT", 1, 1), ("operation_key", "TEXT", 1, 2),
                                 ("request_hash", "BLOB", 1, 0), ("resource_type", "TEXT", 1, 0),
                                 ("resource_id", "TEXT", 1, 0)],
        })
    if version >= 3:
        expected_columns["opportunity_drafts"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
            ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 4:
        expected_columns["task_plans"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
            ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 5:
        expected_columns["order_drafts"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("proposed_amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
            ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 6:
        expected_columns["invoice_drafts"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("order_id", "TEXT", 1, 0),
            ("proposed_amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
            ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 7:
        expected_columns["task_queue"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0), ("priority", "INTEGER", 1, 0),
            ("max_attempts", "INTEGER", 1, 0), ("attempts", "INTEGER", 1, 0),
            ("backoff_seconds", "INTEGER", 1, 0), ("available_at", "INTEGER", 1, 0),
            ("deadline_at", "INTEGER", 1, 0), ("current_lease_id", "TEXT", 0, 0),
            ("lease_until", "INTEGER", 0, 0), ("created_at", "INTEGER", 1, 0),
            ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["queue_leases"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("queue_id", "TEXT", 1, 0),
            ("attempt", "INTEGER", 1, 0), ("lease_until", "INTEGER", 1, 0),
        ]
    if version >= 8:
        expected_columns["task_workflows"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0), ("output_hash", "BLOB", 0, 0),
            ("output_size", "INTEGER", 0, 0), ("created_at", "INTEGER", 1, 0),
            ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 9:
        expected_columns["payment_observations"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("invoice_id", "TEXT", 1, 0),
            ("proposed_amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
            ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 10:
        expected_columns["task_associations"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("queue_id", "TEXT", 1, 0),
            ("workflow_id", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
    if version >= 11:
        expected_columns["paired_completions"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("queue_id", "TEXT", 1, 0),
            ("workflow_id", "TEXT", 1, 0), ("lease_id", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
    if version >= 12:
        expected_columns["managed_task_pairs"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
    if version >= 13:
        expected_columns["financial_assertions"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0),
            ("stage", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
            ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 14:
        expected_columns["sandbox_payments"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("amount_paise", "INTEGER", 1, 0),
            ("currency", "TEXT", 1, 0), ("key_fingerprint", "BLOB", 1, 0), ("stage", "TEXT", 1, 0), ("sequence", "INTEGER", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["sandbox_payment_events"] = [
            ("owner_id", "TEXT", 1, 1), ("event_id", "TEXT", 1, 2), ("payment_id", "TEXT", 1, 0),
            ("event_type", "TEXT", 1, 0), ("sequence", "INTEGER", 1, 0), ("body_hash", "BLOB", 1, 0),
        ]
    if version >= 15:
        expected_columns["opportunities"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("source_url", "TEXT", 1, 0), ("source_type", "TEXT", 1, 0), ("connector_id", "TEXT", 0, 0),
            ("budget_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("deadline_at", "INTEGER", 1, 0),
            ("risk_level", "TEXT", 1, 0), ("skill_tags", "TEXT", 1, 0), ("status", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 16:
        expected_columns["services"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("scope", "TEXT", 1, 0), ("deliverable", "TEXT", 1, 0), ("price_paise", "INTEGER", 1, 0),
            ("currency", "TEXT", 1, 0), ("revisions", "INTEGER", 1, 0), ("status", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["portfolio_items"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("service_id", "TEXT", 1, 0),
            ("title", "TEXT", 1, 0), ("description", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
    if version >= 17:
        expected_columns["products"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0),
            ("description", "TEXT", 1, 0), ("version", "TEXT", 1, 0), ("price_paise", "INTEGER", 1, 0),
            ("currency", "TEXT", 1, 0), ("status", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["customers"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("name", "TEXT", 1, 0),
            ("contact", "TEXT", 1, 0), ("source", "TEXT", 1, 0), ("status", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 18:
        expected_columns["communications"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0),
            ("channel", "TEXT", 1, 0), ("body", "TEXT", 1, 0), ("status", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["ledger_entries"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("kind", "TEXT", 1, 0),
            ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("note", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0),
        ]
    if version >= 19:
        expected_columns["followups"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0),
            ("due_at", "INTEGER", 1, 0), ("note", "TEXT", 1, 0), ("status", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 21:
        expected_columns["payment_provider_orders"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("provider", "TEXT", 1, 0), ("mode", "TEXT", 1, 0),
            ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("provider_order_id", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["payment_provider_events"] = [
            ("owner_id", "TEXT", 1, 0), ("event_id", "TEXT", 1, 1), ("order_id", "TEXT", 1, 0),
            ("event_type", "TEXT", 1, 0), ("amount_paise", "INTEGER", 1, 0), ("raw_body_hash", "BLOB", 1, 0), ("received_at", "INTEGER", 1, 0),
        ]
    if version >= 22:
        expected_columns["ai_approvals"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("tool_name", "TEXT", 1, 0), ("args_json", "TEXT", 1, 0),
            ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("expires_at", "INTEGER", 1, 0), ("decided_at", "INTEGER", 0, 0),
        ]
        expected_columns["ai_emergency"] = [
            ("owner_id", "TEXT", 1, 1), ("stopped", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0), ("reason", "TEXT", 1, 0),
        ]
    if version >= 23:
        expected_columns["project_tasks"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("opportunity_id", "TEXT", 0, 0), ("parent_task_id", "TEXT", 0, 0),
            ("title", "TEXT", 1, 0), ("description", "TEXT", 1, 0), ("priority", "INTEGER", 1, 0), ("deadline_at", "INTEGER", 1, 0),
            ("estimated_duration_seconds", "INTEGER", 1, 0), ("actual_duration_seconds", "INTEGER", 1, 0), ("status", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["task_dependencies"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("task_id", "TEXT", 1, 0), ("depends_on_task_id", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["task_execution_logs"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("task_id", "TEXT", 1, 0), ("message", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
    if version >= 24:
        # v24 adds lease and retry columns to project_tasks
        expected_columns["project_tasks"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("opportunity_id", "TEXT", 0, 0), ("parent_task_id", "TEXT", 0, 0),
            ("title", "TEXT", 1, 0), ("description", "TEXT", 1, 0), ("priority", "INTEGER", 1, 0), ("deadline_at", "INTEGER", 1, 0),
            ("estimated_duration_seconds", "INTEGER", 1, 0), ("actual_duration_seconds", "INTEGER", 1, 0), ("status", "TEXT", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0), ("lease_id", "TEXT", 0, 0), ("lease_until", "INTEGER", 0, 0),
            ("attempts", "INTEGER", 1, 0), ("max_attempts", "INTEGER", 1, 0), ("backoff_seconds", "INTEGER", 1, 0), ("available_at", "INTEGER", 1, 0),
        ]
    if version >= 25:
        expected_columns["service_packages"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("service_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0), ("description", "TEXT", 1, 0),
            ("price_paise", "INTEGER", 1, 0), ("delivery_days", "INTEGER", 1, 0), ("revisions", "INTEGER", 1, 0),
            ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["service_requirements"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("service_id", "TEXT", 1, 0), ("requirement", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["service_attachments"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("service_id", "TEXT", 0, 0), ("portfolio_item_id", "TEXT", 0, 0),
            ("file_name", "TEXT", 1, 0), ("file_hash", "BLOB", 0, 0), ("file_size", "INTEGER", 1, 0), ("mime_type", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["proposals"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("service_id", "TEXT", 1, 0), ("customer_id", "TEXT", 0, 0),
            ("title", "TEXT", 1, 0), ("description", "TEXT", 1, 0), ("price_paise", "INTEGER", 1, 0), ("delivery_days", "INTEGER", 1, 0),
            ("status", "TEXT", 1, 0), ("ai_generated", "INTEGER", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["customer_requests"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("service_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0),
            ("message", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 26:
        expected_columns["product_ideas"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0), ("description", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        expected_columns["product_validations"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("product_id", "TEXT", 1, 0), ("criteria", "TEXT", 1, 0), ("result", "TEXT", 1, 0), ("evidence", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["market_research"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("product_id", "TEXT", 1, 0), ("query", "TEXT", 1, 0), ("findings", "TEXT", 1, 0), ("evidence_url", "TEXT", 1, 0), ("ai_generated", "INTEGER", 1, 0), ("verification", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["pricing_suggestions"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("product_id", "TEXT", 1, 0), ("price_paise", "INTEGER", 1, 0), ("rationale", "TEXT", 1, 0), ("ai_generated", "INTEGER", 1, 0), ("evidence", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["product_versions"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("product_id", "TEXT", 1, 0), ("version", "TEXT", 1, 0), ("notes", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["product_files"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("product_id", "TEXT", 1, 0), ("file_name", "TEXT", 1, 0), ("file_hash", "BLOB", 0, 0), ("file_size", "INTEGER", 1, 0), ("mime_type", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["product_sales"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("product_id", "TEXT", 1, 0), ("quantity", "INTEGER", 1, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("sale_at", "INTEGER", 1, 0), ("evidence_url", "TEXT", 1, 0), ("verification", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["product_deliverables"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("product_id", "TEXT", 1, 0), ("file_name", "TEXT", 1, 0), ("file_hash", "BLOB", 0, 0), ("file_size", "INTEGER", 1, 0), ("mime_type", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["product_documentation"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("product_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0), ("content", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
    if version >= 27:
        expected_columns["customer_notes"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0), ("note", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["customer_requirements"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0), ("requirement", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        if version >= 28:
            expected_columns["customer_orders"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0), ("description", "TEXT", 1, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0), ("due_at", "INTEGER", 0, 0), ("billing_name", "TEXT", 0, 0), ("billing_address", "TEXT", 0, 0), ("tax_rate_bps", "INTEGER", 0, 0), ("tax_amount_paise", "INTEGER", 0, 0),
            ]
        else:
            expected_columns["customer_orders"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0), ("title", "TEXT", 1, 0), ("description", "TEXT", 1, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
            ]
        expected_columns["customer_projects"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0), ("project_task_id", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0),
        ]
        expected_columns["crm_communication_approvals"] = [
            ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("communication_id", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("expires_at", "INTEGER", 1, 0), ("decided_at", "INTEGER", 0, 0),
        ]
        expected_columns["crm_outbound_configs"] = [
            ("owner_id", "TEXT", 1, 1), ("enabled", "INTEGER", 1, 0), ("allowed_channels", "TEXT", 1, 0), ("daily_limit", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
        ]
        if version >= 28:
            expected_columns["tax_configs"] = [
                ("owner_id", "TEXT", 1, 1), ("jurisdiction", "TEXT", 1, 0), ("rate_bps", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
            ]
            expected_columns["billing_profiles"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("customer_id", "TEXT", 1, 0), ("billing_name", "TEXT", 1, 0), ("billing_contact", "TEXT", 1, 0), ("billing_address", "TEXT", 1, 0), ("tax_id", "TEXT", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
            ]
            expected_columns["payment_records"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("invoice_id", "TEXT", 0, 0), ("order_id", "TEXT", 0, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("status", "TEXT", 1, 0), ("provider", "TEXT", 1, 0), ("provider_ref", "TEXT", 1, 0), ("verified", "INTEGER", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
            ]
            expected_columns["expense_records"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("category", "TEXT", 1, 0), ("note", "TEXT", 1, 0), ("incurred_at", "INTEGER", 1, 0), ("verified", "INTEGER", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
            ]
            expected_columns["refund_records"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("payment_id", "TEXT", 1, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("reason", "TEXT", 1, 0), ("verified", "INTEGER", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
            ]
            expected_columns["fee_records"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("payment_id", "TEXT", 0, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0), ("fee_type", "TEXT", 1, 0), ("note", "TEXT", 1, 0), ("verified", "INTEGER", 1, 0), ("created_at", "INTEGER", 1, 0), ("updated_at", "INTEGER", 1, 0),
            ]
    if version >= 20:
        if version >= 28:
            expected_columns["invoices"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("order_id", "TEXT", 1, 0),
                ("invoice_number", "TEXT", 1, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0),
                ("status", "TEXT", 1, 0), ("pdf_hash", "BLOB", 0, 0), ("created_at", "INTEGER", 1, 0),
                ("updated_at", "INTEGER", 1, 0), ("due_at", "INTEGER", 0, 0),
                ("billing_name", "TEXT", 0, 0), ("billing_contact", "TEXT", 0, 0), ("billing_address", "TEXT", 0, 0),
                ("tax_rate_bps", "INTEGER", 0, 0), ("tax_amount_paise", "INTEGER", 0, 0),
            ]
        else:
            expected_columns["invoices"] = [
                ("id", "TEXT", 1, 1), ("owner_id", "TEXT", 1, 0), ("order_id", "TEXT", 1, 0),
                ("invoice_number", "TEXT", 1, 0), ("amount_paise", "INTEGER", 1, 0), ("currency", "TEXT", 1, 0),
                ("status", "TEXT", 1, 0), ("pdf_hash", "BLOB", 0, 0), ("created_at", "INTEGER", 1, 0),
                ("updated_at", "INTEGER", 1, 0),
            ]
    objects = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    expected_objects = [("table", name) for name in expected_columns]
    if version >= 6:
        expected_objects.append(("index", "order_drafts_id_owner_idx"))
    if version >= 9:
        expected_objects.append(("index", "invoice_drafts_id_owner_idx"))
    if version >= 10:
        expected_objects.append(("index", "task_workflows_id_owner_idx"))
    if version >= 11:
        expected_objects.append(("index", "task_associations_id_owner_idx"))
    if version >= 16:
        expected_objects.append(("index", "services_id_owner_idx"))
    if version >= 17:
        expected_objects.append(("index", "products_id_owner_idx"))
    if version >= 18:
        expected_objects.append(("index", "communications_id_owner_idx"))
        expected_objects.append(("index", "ledger_entries_id_owner_idx"))
    if version >= 19:
        expected_objects.append(("index", "customers_id_owner_idx"))
        expected_objects.append(("index", "followups_id_owner_idx"))
    if version >= 21:
        expected_objects.append(("index", "payment_provider_orders_id_owner_idx"))
        expected_objects.append(("index", "payment_provider_orders_provider_order_idx"))
        expected_objects.append(("index", "payment_provider_events_id_owner_idx"))
    if version >= 22:
        expected_objects.append(("index", "ai_approvals_id_owner_idx"))
    if version >= 23:
        expected_objects.append(("index", "project_tasks_id_owner_idx"))
        expected_objects.append(("index", "task_dependencies_task_idx"))
        expected_objects.append(("index", "task_execution_logs_task_idx"))
    if version >= 25:
        expected_objects.append(("index", "service_packages_id_owner_idx"))
        expected_objects.append(("index", "service_requirements_service_idx"))
        expected_objects.append(("index", "service_attachments_service_idx"))
        expected_objects.append(("index", "proposals_id_owner_idx"))
        expected_objects.append(("index", "customer_requests_id_owner_idx"))
    if version >= 26:
        expected_objects.append(("index", "product_ideas_id_owner_idx"))
        expected_objects.append(("index", "product_validations_product_idx"))
        expected_objects.append(("index", "market_research_product_idx"))
        expected_objects.append(("index", "pricing_suggestions_product_idx"))
        expected_objects.append(("index", "product_versions_product_idx"))
        expected_objects.append(("index", "product_files_product_idx"))
        expected_objects.append(("index", "product_sales_product_idx"))
        expected_objects.append(("index", "product_deliverables_product_idx"))
        expected_objects.append(("index", "product_documentation_product_idx"))
    if version >= 27:
        expected_objects.append(("index", "customer_notes_customer_idx"))
        expected_objects.append(("index", "customer_requirements_customer_idx"))
        expected_objects.append(("index", "customer_orders_id_owner_idx"))
        expected_objects.append(("index", "customer_orders_customer_idx"))
        expected_objects.append(("index", "customer_projects_customer_idx"))
        expected_objects.append(("index", "customer_projects_task_idx"))
        expected_objects.append(("index", "crm_communication_approvals_id_owner_idx"))
        expected_objects.append(("index", "crm_communication_approvals_comm_idx"))
        expected_objects.append(("index", "crm_outbound_configs_owner_idx"))
    if version >= 28:
        expected_objects.append(("index", "tax_configs_owner_idx"))
        expected_objects.append(("index", "billing_profiles_id_owner_idx"))
        expected_objects.append(("index", "billing_profiles_customer_idx"))
        expected_objects.append(("index", "payment_records_id_owner_idx"))
        expected_objects.append(("index", "payment_records_invoice_idx"))
        expected_objects.append(("index", "payment_records_status_idx"))
        expected_objects.append(("index", "expense_records_id_owner_idx"))
        expected_objects.append(("index", "refund_records_id_owner_idx"))
        expected_objects.append(("index", "refund_records_payment_idx"))
        expected_objects.append(("index", "fee_records_id_owner_idx"))
        expected_objects.append(("index", "fee_records_payment_idx"))
    if version >= 20:
        expected_objects.append(("index", "invoices_id_owner_idx"))
        expected_objects.append(("index", "invoices_number_owner_idx"))
    if [tuple(row) for row in objects] != sorted(expected_objects, key=lambda item: item[1]):
        raise StateStorageError("State database schema is incompatible.")
    if version >= 6:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='order_drafts_id_owner_idx'").fetchone()
        if row is None or row[0] != _V6_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 9:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='invoice_drafts_id_owner_idx'").fetchone()
        if row is None or row[0] != _V9_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 10:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='task_workflows_id_owner_idx'").fetchone()
        if row is None or row[0] != _V10_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 11:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='task_associations_id_owner_idx'").fetchone()
        if row is None or row[0] != _V11_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 16:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='services_id_owner_idx'").fetchone()
        if row is None or row[0] != _V16_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 17:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='products_id_owner_idx'").fetchone()
        if row is None or row[0] != _V17_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 18:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='communications_id_owner_idx'").fetchone()
        if row is None or row[0] != _V18_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='ledger_entries_id_owner_idx'").fetchone()
        if row is None or row[0] != _V18_LEDGER_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 19:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='customers_id_owner_idx'").fetchone()
        if row is None or row[0] != _V19_CUSTOMER_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='followups_id_owner_idx'").fetchone()
        if row is None or row[0] != _V19_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 21:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='payment_provider_orders_id_owner_idx'").fetchone()
        if row is None or row[0] != _V21_PROVIDER_ORDERS_ID_OWNER_IDX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='payment_provider_orders_provider_order_idx'").fetchone()
        if row is None or row[0] != _V21_PROVIDER_ORDERS_PROVIDER_IDX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='payment_provider_events_id_owner_idx'").fetchone()
        if row is None or row[0] != _V21_PROVIDER_EVENTS_ID_IDX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 22:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='ai_approvals_id_owner_idx'").fetchone()
        if row is None or row[0] != _V22_APPROVAL_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 23:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='project_tasks_id_owner_idx'").fetchone()
        if row is None or row[0] != _V23_PROJECT_TASKS_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='task_dependencies_task_idx'").fetchone()
        if row is None or row[0] != _V23_DEPENDS_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='task_execution_logs_task_idx'").fetchone()
        if row is None or row[0] != _V23_LOGS_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 25:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='service_packages_id_owner_idx'").fetchone()
        if row is None or row[0] != _V25_PACKAGE_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='service_requirements_service_idx'").fetchone()
        if row is None or row[0] != _V25_REQUIREMENT_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='service_attachments_service_idx'").fetchone()
        if row is None or row[0] != _V25_ATTACHMENT_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='proposals_id_owner_idx'").fetchone()
        if row is None or row[0] != _V25_PROPOSAL_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='customer_requests_id_owner_idx'").fetchone()
        if row is None or row[0] != _V25_REQUEST_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 26:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='product_ideas_id_owner_idx'").fetchone()
        if row is None or row[0] != _V26_IDEA_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='product_validations_product_idx'").fetchone()
        if row is None or row[0] != _V26_VALIDATION_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='market_research_product_idx'").fetchone()
        if row is None or row[0] != _V26_RESEARCH_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='pricing_suggestions_product_idx'").fetchone()
        if row is None or row[0] != _V26_PRICING_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='product_versions_product_idx'").fetchone()
        if row is None or row[0] != _V26_VERSION_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='product_files_product_idx'").fetchone()
        if row is None or row[0] != _V26_FILE_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='product_sales_product_idx'").fetchone()
        if row is None or row[0] != _V26_SALES_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='product_deliverables_product_idx'").fetchone()
        if row is None or row[0] != _V26_DELIVERABLE_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='product_documentation_product_idx'").fetchone()
        if row is None or row[0] != _V26_DOCS_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 27:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='customer_notes_customer_idx'").fetchone()
        if row is None or row[0] != _V27_NOTE_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='customer_requirements_customer_idx'").fetchone()
        if row is None or row[0] != _V27_REQUIREMENT_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='customer_orders_id_owner_idx'").fetchone()
        if row is None or row[0] != _V27_ORDER_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='customer_orders_customer_idx'").fetchone()
        if row is None or row[0] != _V27_ORDER_CUSTOMER_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='customer_projects_customer_idx'").fetchone()
        if row is None or row[0] != _V27_PROJECT_CUSTOMER_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='customer_projects_task_idx'").fetchone()
        if row is None or row[0] != _V27_PROJECT_TASK_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='crm_communication_approvals_id_owner_idx'").fetchone()
        if row is None or row[0] != _V27_COMM_APPROVAL_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='crm_communication_approvals_comm_idx'").fetchone()
        if row is None or row[0] != _V27_COMM_APPROVAL_COMM_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='crm_outbound_configs_owner_idx'").fetchone()
        if row is None or row[0] != _V27_OUTBOUND_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        if version >= 28:
            for name, sql in [
                ("tax_configs_owner_idx", _V28_TAX_INDEX),
                ("billing_profiles_id_owner_idx", _V28_BILLING_ID_INDEX),
                ("billing_profiles_customer_idx", _V28_BILLING_CUSTOMER_INDEX),
                ("payment_records_id_owner_idx", _V28_PAYMENT_ID_INDEX),
                ("payment_records_invoice_idx", _V28_PAYMENT_INVOICE_INDEX),
                ("payment_records_status_idx", _V28_PAYMENT_STATUS_INDEX),
                ("expense_records_id_owner_idx", _V28_EXPENSE_ID_INDEX),
                ("refund_records_id_owner_idx", _V28_REFUND_ID_INDEX),
                ("refund_records_payment_idx", _V28_REFUND_PAYMENT_INDEX),
                ("fee_records_id_owner_idx", _V28_FEE_ID_INDEX),
                ("fee_records_payment_idx", _V28_FEE_PAYMENT_INDEX),
            ]:
                row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()
                if row is None or row[0] != sql:
                    raise StateStorageError("State database schema constraints are incompatible.")
    if version >= 20:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='invoices_id_owner_idx'").fetchone()
        if row is None or row[0] != _V20_INVOICE_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='invoices_number_owner_idx'").fetchone()
        if row is None or row[0] != _V20_INVOICE_NUMBER_INDEX:
            raise StateStorageError("State database schema constraints are incompatible.")
    for table, expected in expected_columns.items():
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if [(row[1], row[2], row[3], row[5]) for row in columns] != expected:
            raise StateStorageError("State database schema is incompatible.")
    schemas = []
    if version >= 2:
        schemas.append(_V2_SCHEMA)
    if version >= 3:
        schemas.append(_V3_SCHEMA)
    if version >= 4:
        schemas.append(_V4_SCHEMA)
    if version >= 5:
        schemas.append(_V5_SCHEMA)
    if version >= 6:
        schemas.append(_V6_SCHEMA)
    if version >= 7:
        schemas.append(_V7_SCHEMA)
    if version >= 8:
        schemas.append(_V8_SCHEMA)
    if version >= 9:
        schemas.append(_V9_SCHEMA)
    if version >= 10:
        schemas.append(_V10_SCHEMA)
    if version >= 11:
        schemas.append(_V11_SCHEMA)
    if version >= 12:
        schemas.append(_V12_SCHEMA)
    if version >= 13:
        schemas.append(_V13_SCHEMA)
    if version >= 14:
        schemas.append(_V14_SCHEMA)
    if version >= 15:
        schemas.append(_V15_SCHEMA)
    if version >= 16:
        schemas.append(_V16_SCHEMA)
    if version >= 17:
        schemas.append(_V17_SCHEMA)
    if version >= 18:
        schemas.append(_V18_SCHEMA)
    if version >= 19:
        schemas.append(_V19_SCHEMA)
    if version >= 20:
        schemas.append(_V20_SCHEMA)
    if version >= 21:
        schemas.append(_V21_SCHEMA)
    if version >= 22:
        schemas.append(_V22_SCHEMA)
    if version >= 23:
        schemas.append(_V23_SCHEMA)
    if version >= 24:
        schemas.append(_V24_SCHEMA)
    if version >= 25:
        schemas.append(_V25_SCHEMA)
    if version >= 26:
        schemas.append(_V26_SCHEMA)
    if version >= 27:
        schemas.append(_V27_SCHEMA)
    if version >= 28:
        schemas.append(_V28_SCHEMA)
    for schema in schemas:
        for name, statement in schema.items():
            # v24 alters project_tasks via ALTER TABLE; sql formatting differs from CREATE literal, so rely on PRAGMA table_info instead
            if version >= 24 and name == "project_tasks":
                continue
            if version >= 24 and name == "project_tasks" and statement == _V23_SCHEMA.get("project_tasks"):
                continue
            # v28 alters invoices/customer_orders via ALTER TABLE; rely on PRAGMA table_info instead
            if version >= 28 and name in ("invoices", "customer_orders"):
                continue
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            if row is None or row[0] != statement:
                raise StateStorageError("State database schema constraints are incompatible.")
    versions = conn.execute("SELECT version FROM state_migrations ORDER BY version").fetchall()
    if [row[0] for row in versions] != list(range(1, version + 1)):
        raise StateStorageError("State migration history is incomplete or unsupported.")
    return version
























































def _check_managed_pairs(conn: sqlite3.Connection, owner=None, queue_id=None, workflow_id=None, association_id=None) -> None:
    """Check managed-parent state combinations, not history authenticity."""
    invalid = conn.execute("""
        SELECT 1 FROM managed_task_pairs m
        LEFT JOIN task_associations a ON a.id=m.id AND a.owner_id=m.owner_id
        LEFT JOIN task_queue q ON q.id=a.queue_id AND q.owner_id=m.owner_id
        LEFT JOIN task_workflows w ON w.id=a.workflow_id AND w.owner_id=m.owner_id
        LEFT JOIN paired_completions c ON c.id=m.id AND c.owner_id=m.owner_id
        WHERE (? IS NULL OR m.owner_id=?) AND (? IS NULL OR a.queue_id=?)
          AND (? IS NULL OR a.workflow_id=?) AND (? IS NULL OR m.id=?)
          AND (a.id IS NULL OR q.id IS NULL OR w.id IS NULL OR m.created_at!=a.created_at
               OR NOT (
                    (q.status='queued' AND w.status IN ('planned','in_progress','pending_review') AND c.id IS NULL)
                 OR (q.status='leased' AND w.status IN ('in_progress','pending_review') AND c.id IS NULL)
                 OR (q.status='leased' AND w.status='completed' AND c.id IS NOT NULL)
                 OR (q.status='exhausted' AND w.status IN ('in_progress','pending_review','cancelled') AND c.id IS NULL)
                 OR (q.status='cancelled' AND w.status='cancelled' AND c.id IS NULL)
               )) LIMIT 1
    """, (owner, owner, queue_id, queue_id, workflow_id, workflow_id, association_id, association_id)).fetchone()
    if invalid is not None:
        raise StateStorageError("Managed pair state is inconsistent.")


def _is_managed_pair(conn: sqlite3.Connection, owner: str, *, queue_id=None, workflow_id=None, association_id=None) -> bool:
    """Check an owned marker; not a defense against direct SQLite access."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < 12:
        return False
    _check_managed_pairs(conn, owner, queue_id, workflow_id, association_id)
    orphan = conn.execute("""
        SELECT 1 FROM task_associations a
        LEFT JOIN managed_task_pairs m ON m.id=a.id AND m.owner_id=a.owner_id
        WHERE a.owner_id=? AND (? IS NULL OR a.queue_id=?)
          AND (? IS NULL OR a.workflow_id=?) AND (? IS NULL OR a.id=?)
          AND m.id IS NULL AND (
              EXISTS (SELECT 1 FROM state_operations o WHERE o.owner_id=a.owner_id
                      AND o.resource_id=a.id AND o.resource_type='managed_task_pair')
              OR EXISTS (SELECT 1 FROM state_audit e WHERE e.actor_id=a.owner_id
                         AND e.resource_id=a.id AND e.resource_type='managed_task_pair')
          ) LIMIT 1
    """, (owner, queue_id, queue_id, workflow_id, workflow_id, association_id, association_id)).fetchone()
    if orphan is not None:
        raise StateStorageError("Managed pair marker is unavailable.")
    return conn.execute("""
        SELECT 1 FROM managed_task_pairs m JOIN task_associations a
          ON a.id=m.id AND a.owner_id=m.owner_id
        WHERE m.owner_id=? AND (? IS NULL OR a.queue_id=?)
          AND (? IS NULL OR a.workflow_id=?) AND (? IS NULL OR a.id=?) LIMIT 1
    """, (owner, queue_id, queue_id, workflow_id, workflow_id, association_id, association_id)).fetchone() is not None


def _check_queue_integrity(conn: sqlite3.Connection, *, owner: str | None = None, record_id: str | None = None) -> None:
    """Reject inconsistent queue/lease metadata; never modify the source.

    SQLite's foreign keys do not enforce attempt history or agreement between
    the two lease expiry values. Scoped checks do not inspect unrelated owners.
    """
    bad = conn.execute("""
        SELECT t.id FROM task_queue AS t
        WHERE (? IS NULL OR t.owner_id=?) AND (? IS NULL OR t.id=?) AND (
            t.attempts != (SELECT COUNT(*) FROM queue_leases l WHERE l.queue_id=t.id)
            OR t.attempts != COALESCE((SELECT MAX(l.attempt) FROM queue_leases l WHERE l.queue_id=t.id), 0)
            OR EXISTS (SELECT 1 FROM queue_leases l WHERE l.queue_id=t.id AND l.owner_id!=t.owner_id)
            OR (t.status='queued' AND (t.attempts>=t.max_attempts
                OR (t.deadline_at!=0 AND t.available_at>=t.deadline_at)))
            OR (t.status='exhausted' AND t.attempts=0)
            OR (t.status='leased' AND (t.lease_until<=t.updated_at
                OR (t.deadline_at!=0 AND t.lease_until>t.deadline_at)))
            OR (t.status='leased' AND NOT EXISTS (
                SELECT 1 FROM queue_leases l WHERE l.id=t.current_lease_id
                AND l.queue_id=t.id AND l.owner_id=t.owner_id
                AND l.attempt=t.attempts AND l.lease_until=t.lease_until
            ))
        ) LIMIT 1
    """, (owner, owner, record_id, record_id)).fetchone()
    if bad is not None:
        raise StateStorageError("Queue state and lease history are inconsistent.")


# Fixed, versioned internal mappings only: do not interpolate caller input.
_OPERATION_RESULTS = (
    (3, "opportunity_draft", "opportunity_drafts"),
    (4, "task_plan", "task_plans"),
    (5, "order_draft", "order_drafts"),
    (6, "invoice_draft", "invoice_drafts"),
    (7, "task_queue", "task_queue"),
    (7, "queue_lease", "queue_leases"),
    (8, "task_workflow", "task_workflows"),
    (9, "payment_observation", "payment_observations"),
    (10, "task_association", "task_associations"),
    (11, "paired_completion", "paired_completions"),
    (12, "managed_task_pair", "managed_task_pairs"),
    (13, "financial_assertion", "financial_assertions"),
    (14, "sandbox_payment", "sandbox_payments"),
    (15, "opportunity", "opportunities"),
    (16, "service", "services"),
    (16, "portfolio_item", "portfolio_items"),
    (17, "product", "products"),
    (17, "customer", "customers"),
    (18, "communication", "communications"),
    (18, "ledger_entry", "ledger_entries"),
    (19, "followup", "followups"),
    (20, "invoice", "invoices"),
    (21, "provider_order", "payment_provider_orders"),
    (22, "ai_approval", "ai_approvals"),
)


def _check_operation_results(conn: sqlite3.Connection, version: int) -> None:
    """Check known retry results belong to their operation owner, not authenticity.

    Unknown resource types remain outside this check. A valid reference does
    not prove the result matches the original request or that rows are complete.
    """
    for minimum, kind, table in _OPERATION_RESULTS:
        if version < minimum:
            continue
        missing = conn.execute(
            f"SELECT 1 FROM state_operations AS o WHERE o.resource_type=? "
            f"AND NOT EXISTS (SELECT 1 FROM {table} AS r "
            "WHERE r.id=o.resource_id AND r.owner_id=o.owner_id) LIMIT 1", (kind,)
        ).fetchone()
        if missing is not None:
            raise StateStorageError("State operation result is missing or has a different owner.")


def _check_audit_operation_balance(conn: sqlite3.Connection, version: int) -> None:
    """Compare recognized mutations per owner/result, without asserting authenticity.

    A queue claim remembers a lease ID but audits its parent queue ID. Matching
    counts do not prove entries are correct or that entire pairs were retained.
    """
    for minimum, kind, _table in _OPERATION_RESULTS:
        if version < minimum or kind == "queue_lease":
            continue
        lease_rows = ""
        if kind == "task_queue":
            lease_rows = (
                " UNION ALL SELECT o.owner_id, l.queue_id, 1 FROM state_operations o "
                "JOIN queue_leases l ON l.id=o.resource_id AND l.owner_id=o.owner_id "
                "WHERE o.resource_type='queue_lease'"
            )
        mismatch = conn.execute(
            "SELECT 1 FROM ("
            "SELECT owner_id AS owner, resource_id AS ident, 1 AS delta FROM state_operations WHERE resource_type=? "
            "UNION ALL SELECT actor_id, resource_id, -1 FROM state_audit WHERE resource_type=?"
            + lease_rows + ") GROUP BY owner, ident HAVING SUM(delta) != 0 LIMIT 1",
            (kind, kind),
        ).fetchone()
        if mismatch is not None:
            raise StateStorageError("State audit and operation counts are inconsistent.")


# Each supported service writes exactly one creation event for each record.
# Queue leases are checked separately: their audit identifies the parent queue.
_CREATION_EVENTS = (
    (3, "opportunity_drafts", "opportunity_draft", "draft_created"),
    (4, "task_plans", "task_plan", "task_planned"),
    (5, "order_drafts", "order_draft", "order_draft_created"),
    (6, "invoice_drafts", "invoice_draft", "invoice_draft_created"),
    (7, "task_queue", "task_queue", "queue_enqueued"),
    (8, "task_workflows", "task_workflow", "workflow_created"),
    (9, "payment_observations", "payment_observation", "payment_observation_created"),
    (10, "task_associations", "task_association", "task_association_created"),
    (11, "paired_completions", "paired_completion", "paired_completion_created"),
    (12, "managed_task_pairs", "managed_task_pair", "managed_task_pair_created"),
    (13, "financial_assertions", "financial_assertion", "financial_assertion_created"),
    (14, "sandbox_payments", "sandbox_payment", "sandbox_payment_created"),
    (15, "opportunities", "opportunity", "opportunity_created"),
    (16, "services", "service", "service_created"),
    (16, "portfolio_items", "portfolio_item", "portfolio_item_created"),
    (17, "products", "product", "product_created"),
    (17, "customers", "customer", "customer_created"),
    (18, "communications", "communication", "communication_created"),
    (18, "ledger_entries", "ledger_entry", "ledger_entry_created"),
    (19, "followups", "followup", "followup_created"),
    (20, "invoices", "invoice", "invoice_created"),
    (21, "payment_provider_orders", "provider_order", "provider_order_created"),
    (22, "ai_approvals", "ai_approval", "ai_approval_created"),
)


def _check_creation_events(conn: sqlite3.Connection, version: int) -> None:
    """Check selected audit history consistency, not authenticity or completeness."""
    for minimum, table, kind, event in _CREATION_EVENTS:
        if version < minimum:
            continue
        missing_or_duplicate = conn.execute(
            f"SELECT 1 FROM {table} AS r LEFT JOIN "
            "(SELECT actor_id, resource_id, COUNT(*) AS n FROM state_audit "
            "WHERE resource_type=? AND event=? GROUP BY actor_id, resource_id) AS a "
            "ON a.actor_id=r.owner_id AND a.resource_id=r.id "
            "WHERE COALESCE(a.n, 0) != 1 LIMIT 1", (kind, event),
        ).fetchone()
        if missing_or_duplicate is not None:
            raise StateStorageError("State record creation audit is inconsistent.")
    if version >= 7:
        inconsistent_leases = conn.execute("""
            SELECT 1 FROM task_queue AS q
            LEFT JOIN (SELECT owner_id, queue_id, COUNT(*) AS n FROM queue_leases
                       GROUP BY owner_id, queue_id) AS l
                ON l.owner_id=q.owner_id AND l.queue_id=q.id
            LEFT JOIN (SELECT actor_id, resource_id, COUNT(*) AS n FROM state_audit
                       WHERE resource_type='task_queue' AND event='queue_leased'
                       GROUP BY actor_id, resource_id) AS a
                ON a.actor_id=q.owner_id AND a.resource_id=q.id
            WHERE COALESCE(l.n, 0) != COALESCE(a.n, 0) LIMIT 1
        """).fetchone()
        if inconsistent_leases is not None:
            raise StateStorageError("Queue lease audit history is inconsistent.")


# These four services have exactly one irreversible transition, with no edits.
# Preserve version-specific semantics if a future service adds transitions.
_TERMINAL_TRANSITIONS = (
    (3, "opportunity_drafts", "opportunity_draft", "draft_created", "draft_archived", "archived"),
    (4, "task_plans", "task_plan", "task_planned", "task_cancelled", "cancelled"),
    (5, "order_drafts", "order_draft", "order_draft_created", "order_draft_cancelled", "cancelled"),
    (6, "invoice_drafts", "invoice_draft", "invoice_draft_created", "invoice_draft_cancelled", "cancelled"),
)


def _check_terminal_transition_events(conn: sqlite3.Connection, version: int) -> None:
    """Check selected one-way status/audit consistency, not authenticity.

    Audit IDs establish insertion order; wall clocks can repeat or move. This
    cannot prove history if record and events are consistently altered together.
    """
    for minimum, table, kind, created, terminal, final_status in _TERMINAL_TRANSITIONS:
        if version < minimum:
            continue
        inconsistent = conn.execute(
            f"SELECT 1 FROM {table} AS r LEFT JOIN ("
            "SELECT actor_id, resource_id, "
            "SUM(CASE WHEN event=? THEN 1 ELSE 0 END) AS n, "
            "MIN(CASE WHEN event=? THEN id END) AS terminal_id, "
            "MIN(CASE WHEN event=? THEN id END) AS creation_id "
            "FROM state_audit WHERE resource_type=? AND event IN (?,?) "
            "GROUP BY actor_id, resource_id) AS a "
            "ON a.actor_id=r.owner_id AND a.resource_id=r.id "
            "WHERE COALESCE(a.n, 0) != CASE WHEN r.status=? THEN 1 ELSE 0 END "
            "OR (r.status=? AND (a.creation_id IS NULL OR a.terminal_id<=a.creation_id)) LIMIT 1",
            (terminal, terminal, created, kind, created, terminal, final_status, final_status),
        ).fetchone()
        if inconsistent is not None:
            raise StateStorageError("State terminal transition audit is inconsistent.")


# Versioned service-event transitions. These are consistency checks only; audit
# events are not authenticated and identical cycles can be erased undetected.
_MULTI_STEP_HISTORIES = (
    (7, "task_queue", "task_queue", {
        (None, "queue_enqueued"): "queued",
        ("queued", "queue_leased"): "leased",
        ("leased", "queue_retry"): "queued",
        ("leased", "queue_recovered"): "queued",
        ("leased", "queue_exhausted"): "exhausted",
        ("queued", "queue_cancelled"): "cancelled",
    }),
    (8, "task_workflows", "task_workflow", {
        (None, "workflow_created"): "planned",
        ("planned", "workflow_started"): "in_progress",
        ("planned", "workflow_cancelled"): "cancelled",
        ("in_progress", "workflow_cancelled"): "cancelled",
        ("pending_review", "workflow_cancelled"): "cancelled",
        ("in_progress", "workflow_submitted"): "pending_review",
        ("pending_review", "workflow_rejected"): "in_progress",
        ("pending_review", "workflow_approved"): "completed",
    }),
    (13, "financial_assertions", "financial_assertion", {
        (None, "financial_assertion_created"): "potential",
        ("potential", "financial_assertion_bound"): "potential",
        ("potential", "financial_assertion_quote_asserted"): "quote_asserted",
        ("quote_asserted", "financial_assertion_contract_asserted"): "contract_asserted",
        ("contract_asserted", "financial_assertion_invoice_asserted"): "invoice_asserted",
        ("invoice_asserted", "financial_assertion_initiated_asserted"): "initiated_asserted",
        **{(stage, "financial_assertion_withdrawn"): "withdrawn" for stage in (
            "potential", "quote_asserted", "contract_asserted", "invoice_asserted", "initiated_asserted")},
    }),
    (9, "payment_observations", "payment_observation", {
        (None, "payment_observation_created"): "unverified",
        ("unverified", "payment_observation_flag"): "flagged",
        ("flagged", "payment_observation_unflag"): "unverified",
        ("unverified", "payment_observation_discard"): "discarded",
        ("flagged", "payment_observation_discard"): "discarded",
    }),
    (15, "opportunities", "opportunity", {
        (None, "opportunity_created"): "discovered",
        ("discovered", "opportunity_vetted"): "vetted",
        ("discovered", "opportunity_dismissed"): "dismissed",
        ("discovered", "opportunity_archived"): "archived",
        ("vetted", "opportunity_pursuing"): "pursuing",
        ("vetted", "opportunity_dismissed"): "dismissed",
        ("vetted", "opportunity_archived"): "archived",
        ("pursuing", "opportunity_archived"): "archived",
        ("pursuing", "opportunity_dismissed"): "dismissed",
    }),
    (16, "services", "service", {
        (None, "service_created"): "draft",
        ("draft", "service_published"): "published",
        ("draft", "service_archived"): "archived",
        ("published", "service_archived"): "archived",
    }),
    (17, "products", "product", {
        (None, "product_created"): "draft",
        ("draft", "product_published"): "published",
        ("draft", "product_archived"): "archived",
        ("published", "product_archived"): "archived",
    }),
    (17, "customers", "customer", {
        (None, "customer_created"): "lead",
        ("lead", "customer_contacted"): "contacted",
        ("lead", "customer_rejected"): "rejected",
        ("contacted", "customer_converted"): "converted",
        ("contacted", "customer_rejected"): "rejected",
    }),
    (18, "communications", "communication", {
        (None, "communication_created"): "draft",
        ("draft", "communication_sent"): "sent",
        ("draft", "communication_archived"): "archived",
        ("sent", "communication_archived"): "archived",
    }),
    (19, "followups", "followup", {
        (None, "followup_created"): "pending",
        ("pending", "followup_completed"): "completed",
        ("pending", "followup_cancelled"): "cancelled",
    }),
    (20, "invoices", "invoice", {
        (None, "invoice_created"): "draft",
        ("draft", "invoice_issued"): "issued",
        ("draft", "invoice_voided"): "void",
        ("issued", "invoice_paid"): "paid",
        ("issued", "invoice_overdue"): "overdue",
        ("issued", "invoice_voided"): "void",
        ("overdue", "invoice_paid"): "paid",
        ("overdue", "invoice_voided"): "void",
    }),
    (21, "payment_provider_orders", "provider_order", {
        (None, "provider_order_created"): "created",
        ("created", "provider_order_authorized"): "authorized",
        ("created", "provider_order_failed"): "failed",
        ("authorized", "provider_order_captured"): "captured",
        ("authorized", "provider_order_failed"): "failed",
        ("captured", "provider_order_settled"): "settled",
        ("captured", "provider_order_refunded"): "refunded",
        ("settled", "provider_order_refunded"): "refunded",
        ("captured", "provider_order_failed"): "failed",
    }),
    (22, "ai_approvals", "ai_approval", {
        (None, "ai_approval_created"): "pending",
        ("pending", "ai_approval_approved"): "approved",
        ("pending", "ai_approval_rejected"): "rejected",
        ("pending", "ai_approval_expired"): "expired",
        ("pending", "ai_approval_revoked"): "revoked",
        ("approved", "ai_approval_revoked"): "revoked",
    }),
)


def _check_multistep_histories(conn: sqlite3.Connection, version: int) -> None:
    """Replay recognized event sequences in insertion order, not wall time.

    This checks every owner's records at snapshot/migration checkpoints. It
    does not authenticate events, verify request hashes, or detect erased cycles.
    The fixed table names and transition maps must change with future schemas.
    """
    for minimum, table, kind, transitions in _MULTI_STEP_HISTORIES:
        if version < minimum:
            continue
        status_column = "stage" if table == "financial_assertions" else "status"
        rows = conn.execute(
            f"SELECT r.id, r.{status_column}, a.event FROM {table} AS r "
            "LEFT JOIN state_audit AS a ON a.actor_id=r.owner_id AND a.resource_id=r.id "
            "AND a.resource_type=? ORDER BY r.id, a.id", (kind,),
        )
        previous_id, expected_status, replayed = None, None, None
        for ident, status, event in rows:
            if ident != previous_id:
                if previous_id is not None and replayed != expected_status:
                    raise StateStorageError("State transition history is inconsistent.")
                previous_id, expected_status, replayed = ident, status, None
            step = (replayed, event)
            if step not in transitions:
                raise StateStorageError("State transition history is inconsistent.")
            replayed = transitions[step]
        if previous_id is not None and replayed != expected_status:
            raise StateStorageError("State transition history is inconsistent.")


def _check_proposed_amount_snapshots(conn: sqlite3.Connection, version: int) -> None:
    """Compare immutable draft snapshots; these are not financial evidence.

    Current services cannot edit draft amounts. If that changes, snapshot
    semantics and this version-aware check must be redesigned together.
    Parent status is intentionally independent and does not cascade.
    """
    if version >= 6:
        bad_invoice = conn.execute("""
            SELECT 1 FROM invoice_drafts i JOIN order_drafts o
            ON o.id=i.order_id AND o.owner_id=i.owner_id
            WHERE i.proposed_amount_paise != o.proposed_amount_paise OR i.currency != o.currency
            LIMIT 1
        """).fetchone()
        if bad_invoice is not None:
            raise StateStorageError("Invoice draft amount snapshot is inconsistent.")
    if version >= 9:
        bad_observation = conn.execute("""
            SELECT 1 FROM payment_observations p JOIN invoice_drafts i
            ON i.id=p.invoice_id AND i.owner_id=p.owner_id
            WHERE p.proposed_amount_paise != i.proposed_amount_paise OR p.currency != i.currency
            LIMIT 1
        """).fetchone()
        if bad_observation is not None:
            raise StateStorageError("Unverified placeholder amount snapshot is inconsistent.")


def _check_paired_completions(conn: sqlite3.Connection, owner=None, queue_id=None) -> None:
    """Check local completion relationships, not actual work or audit authenticity."""
    bad = conn.execute("""
        SELECT 1 FROM paired_completions c
        LEFT JOIN task_associations a ON a.id=c.id AND a.owner_id=c.owner_id
        LEFT JOIN task_queue q ON q.id=c.queue_id AND q.owner_id=c.owner_id
        LEFT JOIN task_workflows w ON w.id=c.workflow_id AND w.owner_id=c.owner_id
        LEFT JOIN queue_leases l ON l.id=c.lease_id AND l.queue_id=c.queue_id AND l.owner_id=c.owner_id
        WHERE (? IS NULL OR c.owner_id=?) AND (? IS NULL OR c.queue_id=?)
          AND (a.id IS NULL OR q.id IS NULL OR w.id IS NULL OR l.id IS NULL
               OR a.queue_id!=c.queue_id OR a.workflow_id!=c.workflow_id
               OR q.status!='leased' OR q.current_lease_id!=c.lease_id
               OR w.status!='completed' OR w.updated_at!=c.created_at
               OR c.created_at>=l.lease_until) LIMIT 1
    """, (owner, owner, queue_id, queue_id)).fetchone()
    if bad is not None:
        raise StateStorageError("Paired completion state is inconsistent.")


def _has_paired_completion(conn: sqlite3.Connection, owner: str, queue_id: str) -> bool:
    """Validate the requested v11 queue completion before treating it as closed."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < 11:
        return False
    _check_paired_completions(conn, owner, queue_id)
    return conn.execute("SELECT 1 FROM paired_completions WHERE owner_id=? AND queue_id=?", (owner, queue_id)).fetchone() is not None


def _financial_assertion_request(action: str, values: list) -> bytes:
    value = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    payload = json.dumps([action, value], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).digest()


def _check_financial_assertion_binding(conn: sqlite3.Connection, owner: str, ident: str, title: str, amount: int) -> None:
    """Detect selected mismatches, not coordinated database forgery."""
    creation = _financial_assertion_request("financial_assertion_create", [title, amount, "INR"])
    keys = conn.execute("SELECT operation_key FROM state_operations WHERE owner_id=? AND resource_type='financial_assertion' "
                        "AND resource_id=? AND request_hash=? LIMIT 2", (owner, ident, creation)).fetchall()
    if len(keys) != 1:
        raise StateStorageError("Financial assertion creation binding is inconsistent.")
    binding = _financial_assertion_request("financial_assertion_create_binding", [keys[0][0], ident, title, amount])
    bound = conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type='financial_assertion' "
                         "AND resource_id=? AND request_hash=?", (owner, ident, binding)).fetchone()[0]
    events = conn.execute("SELECT count(*) FROM state_audit WHERE actor_id=? AND resource_type='financial_assertion' "
                          "AND resource_id=? AND event='financial_assertion_bound'", (owner, ident)).fetchone()[0]
    if bound != 1 or events != 1:
        raise StateStorageError("Financial assertion creation binding is inconsistent.")


_SANDBOX_TRANSITIONS = {
    ("sandbox_awaiting", "confirmation"): "sandbox_confirmed",
    ("sandbox_awaiting", "failure"): "sandbox_failed",
    ("sandbox_confirmed", "settlement"): "sandbox_settled",
    ("sandbox_settled", "refund"): "sandbox_refunded",
}


def _check_sandbox_payment_integrity(conn: sqlite3.Connection, owner=None, ident=None) -> None:
    """Check selected synthetic journal links, not signatures or authenticity."""
    payments = conn.execute("SELECT id,owner_id,amount_paise,key_fingerprint,stage,sequence FROM sandbox_payments "
                            "WHERE (? IS NULL OR owner_id=?) AND (? IS NULL OR id=?)",
                            (owner, owner, ident, ident)).fetchall()
    for payment_id, actor, amount, fingerprint, stage, sequence in payments:
        created = _financial_assertion_request("sandbox_payment_create", [amount, fingerprint.hex()])
        keys = conn.execute("SELECT operation_key FROM state_operations WHERE owner_id=? AND resource_type='sandbox_payment' "
                            "AND resource_id=? AND request_hash=? LIMIT 2", (actor, payment_id, created)).fetchall()
        if len(keys) != 1:
            raise StateStorageError("Synthetic payment creation is inconsistent.")
        binding = _financial_assertion_request("sandbox_payment_binding", [keys[0][0], payment_id, amount, fingerprint.hex()])
        if conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type='sandbox_payment' "
                        "AND resource_id=? AND request_hash=?", (actor, payment_id, binding)).fetchone()[0] != 1:
            raise StateStorageError("Synthetic payment binding is inconsistent.")
        events = conn.execute("SELECT event_id,event_type,sequence,body_hash FROM sandbox_payment_events "
                              "WHERE owner_id=? AND payment_id=? ORDER BY sequence", (actor, payment_id)).fetchall()
        audits = [r[0] for r in conn.execute("SELECT event FROM state_audit WHERE actor_id=? AND resource_type='sandbox_payment' "
                                             "AND resource_id=? ORDER BY id", (actor, payment_id))]
        expected = ["sandbox_payment_created", "sandbox_payment_bound"]
        state = "sandbox_awaiting"
        for index, (event_id, kind, seq, digest) in enumerate(events, 1):
            state = _SANDBOX_TRANSITIONS.get((state, kind))
            if state is None or seq != index:
                raise StateStorageError("Synthetic payment event history is inconsistent.")
            request = _financial_assertion_request("sandbox_payment_event", [payment_id, event_id, digest.hex()])
            if conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type='sandbox_payment' "
                            "AND resource_id=? AND request_hash=?", (actor, payment_id, request)).fetchone()[0] != 1:
                raise StateStorageError("Synthetic payment event operation is inconsistent.")
            expected.append("sandbox_payment_" + kind)
        count = conn.execute("SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type='sandbox_payment' "
                             "AND resource_id=?", (actor, payment_id)).fetchone()[0]
        if sequence != len(events) or state != stage or audits != expected or count != len(expected):
            raise StateStorageError("Synthetic payment event history is inconsistent.")


def _check_invoice_withdraw_binding(conn: sqlite3.Connection, owner: str, ident: str) -> None:
    """Check selected local withdrawal operation/audit bindings, not authenticity."""
    payload = json.dumps(["financial_invoice_withdraw", ident], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    digest = hashlib.sha256(payload).digest()
    operation_count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type='invoice_draft' "
        "AND resource_id=? AND request_hash=?", (owner, ident, digest)).fetchone()[0]
    audit_count = conn.execute(
        "SELECT count(*) FROM state_audit WHERE actor_id=? AND resource_type='invoice_draft' "
        "AND resource_id=? AND event='financial_invoice_withdrawn'", (owner, ident)).fetchone()[0]
    if operation_count != audit_count or audit_count > 1:
        raise StateStorageError("Invoice draft withdrawal binding is inconsistent.")


def _check_integrity(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        raise StateStorageError("State database integrity check failed.")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise StateStorageError("State database foreign key check failed.")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 3:
        _check_operation_results(conn, version)
        _check_audit_operation_balance(conn, version)
    if version >= 6:
        _check_proposed_amount_snapshots(conn, version)
    if version >= 9:
        for ident, owner in conn.execute("SELECT id,owner_id FROM invoice_drafts"):
            _check_invoice_withdraw_binding(conn, owner, ident)
    if version >= 7:
        _check_queue_integrity(conn)
    if version >= 3:
        _check_creation_events(conn, version)
        _check_terminal_transition_events(conn, version)
    if version >= 7:
        _check_multistep_histories(conn, version)
    if version >= 11:
        _check_paired_completions(conn)
    if version >= 12:
        _check_managed_pairs(conn)
    if version >= 13:
        for row in conn.execute("SELECT id,owner_id,title,amount_paise FROM financial_assertions"):
            _check_financial_assertion_binding(conn, row[1], row[0], row[2], row[3])
    if version >= 14:
        _check_sandbox_payment_integrity(conn)


def _copy_database(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    source.backup(destination)


class StateStorage:
    """Explicitly initialize or open a distinct, private state database.

    No existing file is initialized or overwritten. This low-level object is
    internal and must not be exposed as an authenticated application service.
    """

    def __init__(self, path: Path, *, create: bool = False, confirmed: bool = False):
        if not isinstance(create, bool):
            raise StateStorageError("Create must be a boolean.")
        if create and confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to create a state database.")
        self._files = _PrivateFiles(path)
        self.path = self._files.path
        if create:
            self._files.create_exclusive()
        try:
            if create:
                with self._files.transaction() as conn:
                    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    conn.execute("CREATE TABLE state_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
                    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION, int(time.time())))
                    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                # Inspect unknown files read-only, before any write transaction.
                self._files._ensure_file()
                with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
                    _validate_schema(conn)
        except (StorageError, sqlite3.Error, OSError) as exc:
            raise StateStorageError("State database is unavailable or incompatible; no migration was performed.") from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize internal writes; recheck identity/version and roll back errors."""
        try:
            with self._files.transaction() as conn:
                _validate_schema(conn)
                yield conn
        except (StorageError, sqlite3.Error, OSError) as exc:
            raise StateStorageError("State transaction could not be completed.") from exc

    def migrate_to_v2(self, backup: Path, *, confirmed: bool = False) -> None:
        """Explicitly upgrade version 1 after a verified non-overwriting backup.

        A write lock is held from validation through backup and commit, so no
        other writer can advance the source between its snapshot and upgrade.
        Backup uses a separate read-only connection before any writes start.
        """
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 1:
                raise StateStorageError("Only version 1 can be upgraded to version 2.")
            self.backup_to(backup, confirmed=True)
            _apply_v2(conn)
            if _validate_schema(conn) != 2:
                raise StateStorageError("Migration produced an incompatible schema.")
            # Validate the new version before committing its DDL and history.
            _check_integrity(conn)

    def migrate_to_v3(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 2 only, after a checked backup while holding its write lock."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 2:
                raise StateStorageError("Only version 2 can be upgraded to version 3.")
            self.backup_to(backup, confirmed=True)
            _apply_v3(conn)
            if _validate_schema(conn) != 3:
                raise StateStorageError("Migration produced an incompatible schema.")
            # Validate the new version before committing its DDL and history.
            _check_integrity(conn)

    def migrate_to_v4(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 3 only, after a checked backup while holding its write lock."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 3:
                raise StateStorageError("Only version 3 can be upgraded to version 4.")
            self.backup_to(backup, confirmed=True)
            _apply_v4(conn)
            if _validate_schema(conn) != 4:
                raise StateStorageError("Migration produced an incompatible schema.")
            # Validate the new version before committing its DDL and history.
            _check_integrity(conn)

    def migrate_to_v5(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 4 only, after a checked backup while holding its write lock."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 4:
                raise StateStorageError("Only version 4 can be upgraded to version 5.")
            self.backup_to(backup, confirmed=True)
            _apply_v5(conn)
            if _validate_schema(conn) != 5:
                raise StateStorageError("Migration produced an incompatible schema.")
            # Validate the new version before committing its DDL and history.
            _check_integrity(conn)

    def migrate_to_v6(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 5 only, after a checked backup while holding its write lock."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 5:
                raise StateStorageError("Only version 5 can be upgraded to version 6.")
            self.backup_to(backup, confirmed=True)
            _apply_v6(conn)
            if _validate_schema(conn) != 6:
                raise StateStorageError("Migration produced an incompatible schema.")
            # Validate the new version before committing its DDL and history.
            _check_integrity(conn)

    def migrate_to_v7(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 6 only, after a checked backup while holding its write lock."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 6:
                raise StateStorageError("Only version 6 can be upgraded to version 7.")
            self.backup_to(backup, confirmed=True)
            _apply_v7(conn)
            if _validate_schema(conn) != 7:
                raise StateStorageError("Migration produced an incompatible schema.")
            # Validate the new version before committing its DDL and history.
            _check_integrity(conn)

    def migrate_to_v8(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 7 only, after a checked backup while holding its write lock."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 7:
                raise StateStorageError("Only version 7 can be upgraded to version 8.")
            self.backup_to(backup, confirmed=True)
            _apply_v8(conn)
            if _validate_schema(conn) != 8:
                raise StateStorageError("Migration produced an incompatible schema.")
            # Validate the new version before committing its DDL and history.
            _check_integrity(conn)

    def migrate_to_v9(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 8 only, after a checked backup while holding its write lock."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 8:
                raise StateStorageError("Only version 8 can be upgraded to version 9.")
            self.backup_to(backup, confirmed=True)
            _apply_v9(conn)
            if _validate_schema(conn) != 9:
                raise StateStorageError("Migration produced an incompatible schema.")
            # Validate the new version before committing its DDL and history.
            _check_integrity(conn)

    def migrate_to_v10(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 9 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 9:
                raise StateStorageError("Only version 9 can be upgraded to version 10.")
            self.backup_to(backup, confirmed=True)
            _apply_v10(conn)
            if _validate_schema(conn) != 10:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v11(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 10 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 10:
                raise StateStorageError("Only version 10 can be upgraded to version 11.")
            self.backup_to(backup, confirmed=True)
            _apply_v11(conn)
            if _validate_schema(conn) != 11:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v12(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 11 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 11:
                raise StateStorageError("Only version 11 can be upgraded to version 12.")
            self.backup_to(backup, confirmed=True)
            _apply_v12(conn)
            if _validate_schema(conn) != 12:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v13(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 12 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 12:
                raise StateStorageError("Only version 12 can be upgraded to version 13.")
            self.backup_to(backup, confirmed=True)
            _apply_v13(conn)
            if _validate_schema(conn) != 13:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v14(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 13 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 13:
                raise StateStorageError("Only version 13 can be upgraded to version 14.")
            self.backup_to(backup, confirmed=True)
            _apply_v14(conn)
            if _validate_schema(conn) != 14:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v15(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 14 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 14:
                raise StateStorageError("Only version 14 can be upgraded to version 15.")
            self.backup_to(backup, confirmed=True)
            _apply_v15(conn)
            if _validate_schema(conn) != 15:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v16(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 15 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 15:
                raise StateStorageError("Only version 15 can be upgraded to version 16.")
            self.backup_to(backup, confirmed=True)
            _apply_v16(conn)
            if _validate_schema(conn) != 16:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v17(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 16 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 16:
                raise StateStorageError("Only version 16 can be upgraded to version 17.")
            self.backup_to(backup, confirmed=True)
            _apply_v17(conn)
            if _validate_schema(conn) != 17:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v18(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 17 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 17:
                raise StateStorageError("Only version 17 can be upgraded to version 18.")
            self.backup_to(backup, confirmed=True)
            _apply_v18(conn)
            if _validate_schema(conn) != 18:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v19(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 18 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 18:
                raise StateStorageError("Only version 18 can be upgraded to version 19.")
            self.backup_to(backup, confirmed=True)
            _apply_v19(conn)
            if _validate_schema(conn) != 19:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v20(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 19 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 19:
                raise StateStorageError("Only version 19 can be upgraded to version 20.")
            self.backup_to(backup, confirmed=True)
            _apply_v20(conn)
            if _validate_schema(conn) != 20:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v21(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 20 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 20:
                raise StateStorageError("Only version 20 can be upgraded to version 21.")
            self.backup_to(backup, confirmed=True)
            _apply_v21(conn)
            if _validate_schema(conn) != 21:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v22(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 21 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 21:
                raise StateStorageError("Only version 21 can be upgraded to version 22.")
            self.backup_to(backup, confirmed=True)
            _apply_v22(conn)
            if _validate_schema(conn) != 22:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v23(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 22 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 22:
                raise StateStorageError("Only version 22 can be upgraded to version 23.")
            self.backup_to(backup, confirmed=True)
            _apply_v23(conn)
            if _validate_schema(conn) != 23:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v24(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 23 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 23:
                raise StateStorageError("Only version 23 can be upgraded to version 24.")
            self.backup_to(backup, confirmed=True)
            _apply_v24(conn)
            if _validate_schema(conn) != 24:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v25(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 24 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 24:
                raise StateStorageError("Only version 24 can be upgraded to version 25.")
            self.backup_to(backup, confirmed=True)
            _apply_v25(conn)
            if _validate_schema(conn) != 25:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v26(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 25 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 25:
                raise StateStorageError("Only version 25 can be upgraded to version 26.")
            self.backup_to(backup, confirmed=True)
            _apply_v26(conn)
            if _validate_schema(conn) != 26:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v27(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 26 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 26:
                raise StateStorageError("Only version 26 can be upgraded to version 27.")
            self.backup_to(backup, confirmed=True)
            _apply_v27(conn)
            if _validate_schema(conn) != 27:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def migrate_to_v28(self, backup: Path, *, confirmed: bool = False) -> None:
        """Upgrade version 27 only after a checked non-overwriting backup."""
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to migrate state.")
        with self.transaction() as conn:
            if _validate_schema(conn) != 27:
                raise StateStorageError("Only version 27 can be upgraded to version 28.")
            self.backup_to(backup, confirmed=True)
            _apply_v28(conn)
            if _validate_schema(conn) != 28:
                raise StateStorageError("Migration produced an incompatible schema.")
            _check_integrity(conn)

    def backup_to(self, destination: Path, *, confirmed: bool = False) -> None:
        """Create a verified SQLite copy at a new private path, never overwrite.

        The same operation copies a verified backup to a new restore path.
        Failed operations leave any newly created partial file for inspection.
        """
        if confirmed is not True:
            raise StateStorageError("Explicit confirmation is required to create a backup or restore copy.")
        target = _PrivateFiles(destination)
        try:
            self._files._ensure_file()
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5)) as source:
                source.execute("BEGIN")
                _validate_schema(source)
                _check_integrity(source)
                target.create_exclusive()
                with closing(sqlite3.connect(target.path.as_uri() + "?mode=rw", uri=True, timeout=5)) as output:
                    _copy_database(source, output)
                    _validate_schema(output)
                    _check_integrity(output)
        except (StorageError, sqlite3.Error, OSError) as exc:
            raise StateStorageError("State backup or restore failed; do not use an incomplete copy.") from exc
