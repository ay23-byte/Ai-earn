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
LATEST_SCHEMA_VERSION = 5


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


def _validate_schema(conn: sqlite3.Connection) -> int:
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if application_id != APPLICATION_ID or version not in (1, 2, 3, 4, 5):
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
    objects = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    if [tuple(row) for row in objects] != [("table", name) for name in sorted(expected_columns)]:
        raise StateStorageError("State database schema is incompatible.")
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


def _check_integrity(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        raise StateStorageError("State database integrity check failed.")


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
