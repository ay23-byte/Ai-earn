"""Versioned, private local storage for Phase 2 authentication only."""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from typing import Iterator


SCHEMA_VERSION = 1
# P0-5 display_name added as nullable, 1..64 chars, whitespace trimmed, never used for isolation
_SCHEMA = (
    """CREATE TABLE accounts (
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
        display_name TEXT CHECK(display_name IS NULL OR (length(display_name) BETWEEN 1 AND 64 AND length(trim(display_name)) > 0)),
        password_salt BLOB NOT NULL, password_hash BLOB NOT NULL,
        password_iterations INTEGER NOT NULL, recovery_hash BLOB NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE sessions (
        token_hash BLOB PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
        expires_at INTEGER NOT NULL, revoked_at INTEGER
    )""",
    """CREATE TABLE auth_attempts (
        action TEXT NOT NULL, subject TEXT NOT NULL, occurred_at INTEGER NOT NULL
    )""",
    "CREATE INDEX auth_attempts_lookup ON auth_attempts(action, subject, occurred_at)",
    """CREATE TABLE auth_audit (
        id INTEGER PRIMARY KEY, occurred_at INTEGER NOT NULL,
        event TEXT NOT NULL, account_id TEXT REFERENCES accounts(id)
    )""",
)


def _current_euid() -> int | None:
    """Effective UID on POSIX; None where the concept does not exist (Windows)."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if callable(geteuid) else None


class StorageError(Exception):
    """Database is missing, inaccessible, unsafe, or incompatible."""


def _windows_auth_verified() -> bool:
    # Experimental offline path, backed by reported Windows ACL and candidate
    # verification. The checks below still run for each operation; this is not
    # production approval or an OS security boundary.
    return True


class AuthStorage:
    def __init__(self, path: Path, *, create: bool = True):
        if os.name != "posix" and not (os.name == "nt" and _windows_auth_verified()):
            raise StorageError("Authentication is disabled on this platform until filesystem protections are verified.")
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise StorageError("Authentication database path must be absolute.")
        self._check_parent()
        self._ensure_file(create=create)
        try:
            with self.transaction() as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if version == 0 and not tables:
                    if not create:
                        raise StorageError("Authentication database is not initialized; no changes made.")
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise StorageError("Unsupported authentication database schema; no changes made.")
                # P0-5: ensure display_name column exists for existing v1 DBs (version stays 1, no bump)
                try:
                    cols = [row[1] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()]
                    if "display_name" not in cols:
                        connection.execute("ALTER TABLE accounts ADD COLUMN display_name TEXT CHECK(display_name IS NULL OR (length(display_name) BETWEEN 1 AND 64 AND length(trim(display_name)) > 0))")
                except sqlite3.Error:
                    pass
        except sqlite3.Error as exc:
            raise StorageError("Authentication database could not be initialized.") from exc

    def _check_parent(self) -> None:
        if os.name == "nt":
            from .windows_security import WindowsSecurityError, verify_private_path
            try:
                verify_private_path(self.path.parent, directory=True)
            except WindowsSecurityError as exc:
                raise StorageError("Authentication database parent directory is not private.") from exc
            return
        try:
            info = self.path.parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != _current_euid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise StorageError("Authentication database parent must be an owner-only directory.")
        except OSError as exc:
            raise StorageError("Authentication database parent directory is unavailable.") from exc

    def _ensure_file(self, create: bool = False) -> None:
        self._check_parent()
        try:
            if create:
                if os.name == "nt":
                    from .windows_security import create_private_file
                    try:
                        create_private_file(self.path)
                    except FileExistsError:
                        # Existing files are never trusted based on existence;
                        # the lstat/type and ACL checks below still run.
                        pass
                else:
                    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    try:
                        fd = os.open(self.path, flags, 0o600)
                    except FileExistsError:
                        pass
                    else:
                        os.close(fd)
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise StorageError("Authentication database must be a regular file, not a link.")
            if os.name == "nt":
                from .windows_security import WindowsSecurityError, verify_private_path
                try:
                    verify_private_path(self.path, directory=False)
                except WindowsSecurityError as exc:
                    raise StorageError("Authentication database Windows ACL is unsafe.") from exc
            elif info.st_uid != _current_euid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise StorageError("Authentication database must be owned by and accessible only to the current user.")
        except OSError as exc:
            raise StorageError("Authentication database is missing or could not be opened securely.") from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._ensure_file()
        connection = None
        try:
            # SQLite's normal open mode would recreate a file removed after the
            # path check. Use read/write-existing mode for every transaction.
            connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
