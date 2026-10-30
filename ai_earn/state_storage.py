"""Internal Phase 3 SQLite foundation; no business API or authorization.

This database is distinct from the authentication database. Callers must not
use raw storage as a substitute for session-bound authorization. This module
contains only versioned infrastructure metadata, not customer or payment data.
"""

from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterator

from .auth_storage import AuthStorage, StorageError, _windows_auth_verified


APPLICATION_ID = 0x41494533  # AIE3, distinct from the auth-only database.
SCHEMA_VERSION = 1


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


def _validate_schema(conn: sqlite3.Connection) -> None:
    application_id = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if application_id != APPLICATION_ID or version != SCHEMA_VERSION:
        raise StateStorageError("Unsupported or uninitialized state database; no migration was performed.")
    objects = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    if [tuple(row) for row in objects] != [("table", "state_meta"), ("table", "state_migrations")]:
        raise StateStorageError("State database schema is incompatible.")
    for table, expected in (
        ("state_meta", [("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)]),
        ("state_migrations", [("version", "INTEGER", 0, 1), ("applied_at", "INTEGER", 1, 0)]),
    ):
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if [(row[1], row[2], row[3], row[5]) for row in columns] != expected:
            raise StateStorageError("State database schema is incompatible.")
    versions = conn.execute("SELECT version FROM state_migrations ORDER BY version").fetchall()
    if [row[0] for row in versions] != [SCHEMA_VERSION]:
        raise StateStorageError("State migration history is incomplete or unsupported.")


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
