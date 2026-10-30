"""Internal Phase 3 SQLite foundation; no business API or authorization.

This database is distinct from the authentication database. Callers must not
use raw storage as a substitute for session-bound authorization. This module
contains experimental drafts and task plans, but no customer or payment data.
"""

from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator

from .auth_storage import AuthStorage, StorageError, _windows_auth_verified


APPLICATION_ID = 0x41494533  # AIE3, distinct from the auth-only database.
SCHEMA_VERSION = 1  # New files still bootstrap as version 1; upgrades are explicit.
LATEST_SCHEMA_VERSION = 10


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
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
            self._ensure_file()
        except (OSError, StorageError) as exc:
            # A file created before a later error is left for inspection.
            raise StateStorageError("State file could not be created safely; existing files are never overwritten.") from exc


_V2_SCHEMA = {
    "state_audit": """CREATE TABLE state_audit (
    id INTEGER PRIMARY KEY,
    occurred_at INTEGER NOT NULL CHECK(typeof(occurred_at)='integer' AND occurred_at >= 0),
    actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 1 AND 128),
    event TEXT NOT NULL CHECK(length(event) BETWEEN 1 AND 64),
    resource_type TEXT NOT NULL CHECK(length(resource_type) BETWEEN 1 AND 64),
    resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 128)
)""",
    "state_operations": """CREATE TABLE state_operations (
    owner_id TEXT NOT NULL CHECK(length(owner_id) BETWEEN 1 AND 128),
    operation_key TEXT NOT NULL CHECK(length(operation_key) BETWEEN 1 AND 128),
    request_hash BLOB NOT NULL CHECK(typeof(request_hash)='blob' AND length(request_hash)=32),
    resource_type TEXT NOT NULL CHECK(length(resource_type) BETWEEN 1 AND 64),
    resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 128),
    PRIMARY KEY(owner_id, operation_key)
)""",
}


_V3_SCHEMA = {
    "opportunity_drafts": """CREATE TABLE opportunity_drafts (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    status TEXT NOT NULL CHECK(status IN ('draft', 'archived')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
}


_V4_SCHEMA = {
    "task_plans": """CREATE TABLE task_plans (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    status TEXT NOT NULL CHECK(status IN ('planned', 'cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
}


_V5_SCHEMA = {
    "order_drafts": """CREATE TABLE order_drafts (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    proposed_amount_paise INTEGER NOT NULL CHECK(typeof(proposed_amount_paise)='integer' AND proposed_amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('draft', 'cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at)
)""",
}


_V6_INDEX = "CREATE UNIQUE INDEX order_drafts_id_owner_idx ON order_drafts(id, owner_id)"


_V6_SCHEMA = {
    "invoice_drafts": """CREATE TABLE invoice_drafts (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    order_id TEXT NOT NULL CHECK(length(order_id)=32 AND order_id NOT GLOB '*[^0-9a-f]*'),
    proposed_amount_paise INTEGER NOT NULL CHECK(typeof(proposed_amount_paise)='integer' AND proposed_amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('draft', 'cancelled')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(order_id, owner_id) REFERENCES order_drafts(id, owner_id)
)""",
}


_V7_SCHEMA = {
    "task_queue": """CREATE TABLE task_queue (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    status TEXT NOT NULL CHECK(status IN ('queued', 'leased', 'cancelled', 'exhausted')),
    priority INTEGER NOT NULL CHECK(typeof(priority)='integer' AND priority BETWEEN 0 AND 9),
    max_attempts INTEGER NOT NULL CHECK(typeof(max_attempts)='integer' AND max_attempts BETWEEN 1 AND 10),
    attempts INTEGER NOT NULL CHECK(typeof(attempts)='integer' AND attempts BETWEEN 0 AND max_attempts),
    backoff_seconds INTEGER NOT NULL CHECK(typeof(backoff_seconds)='integer' AND backoff_seconds BETWEEN 0 AND 86400),
    available_at INTEGER NOT NULL CHECK(typeof(available_at)='integer' AND available_at BETWEEN 0 AND 4102444800),
    deadline_at INTEGER NOT NULL CHECK(typeof(deadline_at)='integer' AND deadline_at BETWEEN 0 AND 4102444800),
    current_lease_id TEXT CHECK(current_lease_id IS NULL OR (length(current_lease_id)=32 AND current_lease_id NOT GLOB '*[^0-9a-f]*')),
    lease_until INTEGER CHECK(lease_until IS NULL OR (typeof(lease_until)='integer' AND lease_until BETWEEN 0 AND 4102444800)),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at BETWEEN 0 AND 4102444800),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at BETWEEN created_at AND 4102444800),
    CHECK((status='leased' AND current_lease_id IS NOT NULL AND lease_until IS NOT NULL) OR
          (status!='leased' AND current_lease_id IS NULL AND lease_until IS NULL)),
    UNIQUE(id, owner_id),
    FOREIGN KEY(current_lease_id, id, owner_id) REFERENCES queue_leases(id, queue_id, owner_id)
)""",
    "queue_leases": """CREATE TABLE queue_leases (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    queue_id TEXT NOT NULL CHECK(length(queue_id)=32 AND queue_id NOT GLOB '*[^0-9a-f]*'),
    attempt INTEGER NOT NULL CHECK(typeof(attempt)='integer' AND attempt BETWEEN 1 AND 10),
    lease_until INTEGER NOT NULL CHECK(typeof(lease_until)='integer' AND lease_until BETWEEN 0 AND 4102444800),
    FOREIGN KEY(queue_id, owner_id) REFERENCES task_queue(id, owner_id),
    UNIQUE(queue_id, attempt),
    UNIQUE(id, queue_id, owner_id)
)""",
}


_V8_SCHEMA = {
    "task_workflows": """CREATE TABLE task_workflows (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200 AND length(trim(title)) > 0),
    status TEXT NOT NULL CHECK(status IN ('planned', 'in_progress', 'pending_review', 'completed', 'cancelled')),
    output_hash BLOB,
    output_size INTEGER,
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    CHECK((status IN ('pending_review', 'completed') AND typeof(output_hash)='blob' AND length(output_hash)=32
           AND typeof(output_size)='integer' AND output_size BETWEEN 1 AND 4096) OR
          (status NOT IN ('pending_review', 'completed') AND output_hash IS NULL AND output_size IS NULL))
)""",
}


_V9_INDEX = "CREATE UNIQUE INDEX invoice_drafts_id_owner_idx ON invoice_drafts(id, owner_id)"


_V9_SCHEMA = {
    "payment_observations": """CREATE TABLE payment_observations (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    invoice_id TEXT NOT NULL CHECK(length(invoice_id)=32 AND invoice_id NOT GLOB '*[^0-9a-f]*'),
    proposed_amount_paise INTEGER NOT NULL CHECK(typeof(proposed_amount_paise)='integer' AND proposed_amount_paise BETWEEN 0 AND 1000000000000),
    currency TEXT NOT NULL CHECK(currency='INR'),
    status TEXT NOT NULL CHECK(status IN ('unverified', 'flagged', 'discarded')),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    updated_at INTEGER NOT NULL CHECK(typeof(updated_at)='integer' AND updated_at >= created_at),
    FOREIGN KEY(invoice_id, owner_id) REFERENCES invoice_drafts(id, owner_id)
)""",
}


_V10_INDEX = "CREATE UNIQUE INDEX task_workflows_id_owner_idx ON task_workflows(id, owner_id)"


_V10_SCHEMA = {
    "task_associations": """CREATE TABLE task_associations (
    id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
    owner_id TEXT NOT NULL CHECK(length(owner_id)=32 AND owner_id NOT GLOB '*[^0-9a-f]*'),
    queue_id TEXT NOT NULL CHECK(length(queue_id)=32 AND queue_id NOT GLOB '*[^0-9a-f]*'),
    workflow_id TEXT NOT NULL CHECK(length(workflow_id)=32 AND workflow_id NOT GLOB '*[^0-9a-f]*'),
    created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at >= 0),
    UNIQUE(queue_id, owner_id),
    UNIQUE(workflow_id, owner_id),
    FOREIGN KEY(queue_id, owner_id) REFERENCES task_queue(id, owner_id),
    FOREIGN KEY(workflow_id, owner_id) REFERENCES task_workflows(id, owner_id)
)""",
}


def _validate_schema(conn: sqlite3.Connection) -> int:
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if application_id != APPLICATION_ID or version not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
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
    for schema in schemas:
        for name, statement in schema.items():
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
            if row is None or row[0] != statement:
                raise StateStorageError("State database schema constraints are incompatible.")
    versions = conn.execute("SELECT version FROM state_migrations ORDER BY version").fetchall()
    if [row[0] for row in versions] != list(range(1, version + 1)):
        raise StateStorageError("State migration history is incomplete or unsupported.")
    return version


def _apply_v2(conn: sqlite3.Connection) -> None:
    # Infrastructure only; these tables do not provide authenticated actions,
    # complete business audit, or idempotency semantics by themselves.
    for statement in _V2_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (2, int(time.time())))
    conn.execute("PRAGMA user_version = 2")


def _apply_v3(conn: sqlite3.Connection) -> None:
    for statement in _V3_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (3, int(time.time())))
    conn.execute("PRAGMA user_version = 3")


def _apply_v4(conn: sqlite3.Connection) -> None:
    for statement in _V4_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (4, int(time.time())))
    conn.execute("PRAGMA user_version = 4")


def _apply_v5(conn: sqlite3.Connection) -> None:
    for statement in _V5_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (5, int(time.time())))
    conn.execute("PRAGMA user_version = 5")


def _apply_v6(conn: sqlite3.Connection) -> None:
    conn.execute(_V6_INDEX)
    for statement in _V6_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (6, int(time.time())))
    conn.execute("PRAGMA user_version = 6")


def _apply_v7(conn: sqlite3.Connection) -> None:
    for statement in _V7_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (7, int(time.time())))
    conn.execute("PRAGMA user_version = 7")


def _apply_v8(conn: sqlite3.Connection) -> None:
    for statement in _V8_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (8, int(time.time())))
    conn.execute("PRAGMA user_version = 8")


def _apply_v9(conn: sqlite3.Connection) -> None:
    conn.execute(_V9_INDEX)
    for statement in _V9_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (9, int(time.time())))
    conn.execute("PRAGMA user_version = 9")


def _apply_v10(conn: sqlite3.Connection) -> None:
    conn.execute(_V10_INDEX)
    for statement in _V10_SCHEMA.values():
        conn.execute(statement)
    conn.execute("INSERT INTO state_migrations(version, applied_at) VALUES (?, ?)", (10, int(time.time())))
    conn.execute("PRAGMA user_version = 10")


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
    (9, "payment_observations", "payment_observation", {
        (None, "payment_observation_created"): "unverified",
        ("unverified", "payment_observation_flag"): "flagged",
        ("flagged", "payment_observation_unflag"): "unverified",
        ("unverified", "payment_observation_discard"): "discarded",
        ("flagged", "payment_observation_discard"): "discarded",
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
        rows = conn.execute(
            f"SELECT r.id, r.status, a.event FROM {table} AS r "
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
    if version >= 7:
        _check_queue_integrity(conn)
    if version >= 3:
        _check_creation_events(conn, version)
        _check_terminal_transition_events(conn, version)
    if version >= 7:
        _check_multistep_histories(conn, version)


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
