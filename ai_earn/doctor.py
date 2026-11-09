"""Read-only environment and database diagnostics for local setup recovery.

This module inspects configuration and existing SQLite stores without ever
creating, migrating, replacing or truncating a database. It reports distinct,
actionable statuses so a missing state database can be recovered deliberately.
No password, hash, token or recovery secret is read or displayed.
"""
from dataclasses import dataclass, replace
import os
from pathlib import Path
import sqlite3
from typing import Mapping

from .config import load_settings
from .state_storage import APPLICATION_ID

MISSING = "missing"
NOT_CONFIGURED = "not_configured"
RELATIVE_PATH = "relative_path"
INVALID_PATH = "invalid_path"
PERMISSION_ERROR = "permission_error"
EMPTY_FILE = "empty_file"
NOT_A_DATABASE = "not_a_database"
UNSUPPORTED_SCHEMA = "unsupported_schema"
INCOMPATIBLE_SCHEMA = "incompatible_schema"
OK = "ok"

AUTH_VARIABLE = "AI_EARN_AUTH_DB"
STATE_VARIABLE = "AI_EARN_STATE_DB"
SUPPORTED_STATE_VERSIONS = frozenset(range(1, 15))


@dataclass(frozen=True)
class StoreReport:
    """Status of one configured database; never includes secrets."""

    variable: str
    status: str
    detail: str
    schema_version: int | None = None
    account_count: int | None = None


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = "file:" + path.as_posix() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=5)


def _inspect_auth(connection: sqlite3.Connection) -> StoreReport:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if version != 1 or "accounts" not in tables:
        return StoreReport(AUTH_VARIABLE, UNSUPPORTED_SCHEMA,
                           f"Expected authentication schema version 1, found {version}.",
                           schema_version=version)
    count = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    if count == 0:
        return StoreReport(AUTH_VARIABLE, OK,
                           "Authentication store is intact but contains no account.",
                           schema_version=version, account_count=0)
    if count > 1:
        return StoreReport(AUTH_VARIABLE, INCOMPATIBLE_SCHEMA,
                           "Multiple accounts share this authentication database.",
                           schema_version=version, account_count=count)
    return StoreReport(AUTH_VARIABLE, OK, "Authentication store holds exactly one account.",
                       schema_version=version, account_count=count)


def _inspect_state(connection: sqlite3.Connection) -> StoreReport:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    if application_id != APPLICATION_ID:
        return StoreReport(STATE_VARIABLE, NOT_A_DATABASE,
                           "The file is not an Earn OS state database.",
                           schema_version=version)
    if version not in SUPPORTED_STATE_VERSIONS:
        return StoreReport(STATE_VARIABLE, UNSUPPORTED_SCHEMA,
                           f"State schema version {version} is unsupported.",
                           schema_version=version)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "state_migrations" not in tables:
        return StoreReport(STATE_VARIABLE, INCOMPATIBLE_SCHEMA,
                           "State migration history table is absent.",
                           schema_version=version)
    history = [
        row[0]
        for row in connection.execute(
            "SELECT version FROM state_migrations ORDER BY version"
        ).fetchall()
    ]
    if history != list(range(1, version + 1)):
        return StoreReport(STATE_VARIABLE, INCOMPATIBLE_SCHEMA,
                           "State migration history is incomplete.",
                           schema_version=version)
    if version < 2:
        return StoreReport(STATE_VARIABLE, UNSUPPORTED_SCHEMA,
                           "State database must be migrated to version 2 or newer "
                           "before the dashboard or tracker can use it.",
                           schema_version=version)
    return StoreReport(STATE_VARIABLE, OK, "State store is intact.",
                       schema_version=version)


def _classify(variable: str, value: str) -> StoreReport:
    if not value:
        return StoreReport(variable, NOT_CONFIGURED,
                           "Set this variable to an absolute path inside a private directory.")
    try:
        candidate = Path(value).expanduser()
    except (OSError, RuntimeError, ValueError):
        return StoreReport(variable, INVALID_PATH, "The configured path cannot be resolved.")
    if not candidate.is_absolute():
        return StoreReport(variable, RELATIVE_PATH, "The configured path must be absolute.")
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return StoreReport(variable, MISSING, "The database file does not exist at the configured path.")
    except OSError:
        return StoreReport(variable, PERMISSION_ERROR,
                           "The path could not be inspected; check permissions.")
    if candidate.is_symlink() or not os.path.isfile(candidate):
        return StoreReport(variable, INVALID_PATH,
                           "The path must be a regular file, not a link or directory.")
    if info.st_size == 0:
        return StoreReport(variable, EMPTY_FILE,
                           "The file exists but is empty; it was never initialized.")
    try:
        connection = _open_read_only(candidate)
    except sqlite3.Error:
        return StoreReport(variable, PERMISSION_ERROR,
                           "The database could not be opened read-only.")
    try:
        try:
            verdict = connection.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError:
            return StoreReport(variable, NOT_A_DATABASE,
                               "The file is not a readable SQLite database.")
        if verdict != "ok":
            return StoreReport(variable, NOT_A_DATABASE,
                               "SQLite reported a damaged database file.")
        if variable == AUTH_VARIABLE:
            return _inspect_auth(connection)
        return _inspect_state(connection)
    except sqlite3.Error:
        return StoreReport(variable, NOT_A_DATABASE,
                           "The database could not be validated read-only.")
    finally:
        connection.close()


def diagnose(environ: Mapping[str, str]) -> dict:
    """Inspect configuration and both stores; create, modify or delete nothing."""
    report: dict = {"environment": None, "problems": [], "actions": []}
    try:
        settings = load_settings(environ)
        report["environment"] = settings.environment
    except Exception:
        report["problems"].append("AI_EARN_ENV or AI_EARN_LOG_LEVEL is invalid.")
    auth = _classify(AUTH_VARIABLE, environ.get(AUTH_VARIABLE, ""))
    state = _classify(STATE_VARIABLE, environ.get(STATE_VARIABLE, ""))
    report["auth"] = auth
    report["state"] = state
    if auth.status == NOT_CONFIGURED or state.status == NOT_CONFIGURED:
        report["actions"].append(
            "Point AI_EARN_AUTH_DB and AI_EARN_STATE_DB at absolute paths in one private directory."
        )
    if auth.status == MISSING:
        report["actions"].append(
            "The authentication database is missing. Restore it from a known-good backup; "
            "do not register a replacement account over an old path."
        )
    if state.status == MISSING:
        report["actions"].append(
            "The state database is missing while the account store may still exist. "
            "Run 'python -m ai_earn doctor --plan-init' to review the explicit, "
            "confirmation-required initialization steps."
        )
    if auth.status in (UNSUPPORTED_SCHEMA, INCOMPATIBLE_SCHEMA, NOT_A_DATABASE, EMPTY_FILE):
        report["actions"].append(
            "Do not modify the authentication database automatically; inspect or restore it first."
        )
    if state.status in (UNSUPPORTED_SCHEMA, INCOMPATIBLE_SCHEMA, NOT_A_DATABASE, EMPTY_FILE):
        report["actions"].append(
            "Inspect the state database before any approved migration or initialization."
        )
    if auth.status == OK and state.status == OK:
        if auth.schema_version is not None and state.schema_version is not None and state.schema_version < 2:
            report["actions"].append(
                "Migrate the state database with an explicitly approved migration command before use."
            )
        else:
            report["actions"].append("Stores look usable. Run 'python -m ai_earn dashboard snapshot'.")
    return report
