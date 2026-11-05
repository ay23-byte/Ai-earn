"""Authenticated, local-only dashboard snapshot export.

The browser never receives an authentication token and no HTTP API is created.
Snapshots contain aggregate metrics only and are written beside the private
state database using exclusive file creation.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from .auth import AuthError, AuthService
from .auth_cli import _secret
from .auth_storage import AuthStorage, StorageError
from .config import ConfigurationError, load_settings
from .dashboard_metrics import DashboardMetrics
from .state_storage import StateStorage, StateStorageError


def write_snapshot(path: Path, payload: dict) -> None:
    """Create a private JSON snapshot without ever overwriting an existing file."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path = Path(path)
    if os.name == "nt":
        from .windows_security import create_private_file
        create_private_file(path)
        # The containing directory was validated by StateStorage. The file is
        # created with an explicit current-user/SYSTEM DACL before it is opened.
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
        return

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def run(action, environ, stdin, out, err) -> int:
    if action != "snapshot":
        err.write("Usage: python -m ai_earn dashboard snapshot\n")
        return 2
    if not stdin.isatty() or not out.isatty():
        err.write("Dashboard export requires an interactive terminal; credentials are never accepted from pipes.\n")
        return 2

    auth = None
    token = None
    try:
        settings = load_settings(environ)
        if settings.environment == "production":
            raise ConfigurationError("AI_EARN_ENV")
        auth_path = environ.get("AI_EARN_AUTH_DB", "")
        state_path = environ.get("AI_EARN_STATE_DB", "")
        if not auth_path or not state_path:
            raise StateStorageError("Set AI_EARN_AUTH_DB and AI_EARN_STATE_DB to absolute paths in a private directory.")

        # Open only existing stores. No database creation or migration occurs.
        state = StateStorage(Path(state_path), create=False)
        auth = AuthService(AuthStorage(Path(auth_path), create=False))
        out.write("Username: ")
        out.flush()
        username = stdin.readline().rstrip("\r\n")
        password = _secret("Password: ", err)
        token = auth.login(username, password)

        metrics = DashboardMetrics(auth, state).get(token)
        snapshot = {
            "format": "ai-earn-dashboard-snapshot",
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "metrics": metrics,
        }
        # End the session before writing the export. The JSON contains no token,
        # username, record titles, or customer details.
        auth.logout(token)
        token = None

        stamp = int(time.time())
        destination = state.path.parent / f"dashboard-snapshot-{stamp}-{secrets.token_hex(4)}.json"
        write_snapshot(destination, snapshot)
        out.write("\nDashboard snapshot created successfully.\n")
        out.write(f"File: {destination}\n")
        out.write("Open dashboard/index.html, choose Load snapshot, and select this file.\n")
        out.write("The snapshot is read-only and contains aggregate local metrics only.\n")
        return 0
    except (AuthError, StorageError, StateStorageError, ConfigurationError):
        err.write("Dashboard snapshot could not be created. Check your credentials, private database paths and supported schema; no database was initialized or migrated.\n")
        return 2
    except (OSError, sqlite3.Error, ValueError):
        err.write("Dashboard snapshot could not be written. Inspect the destination directory before retrying; existing files are never overwritten.\n")
        return 1
    except (EOFError, KeyboardInterrupt):
        err.write("Dashboard export cancelled. No external action was performed.\n")
        return 2
    except Exception:
        err.write("Dashboard export encountered an unexpected error. No external action was performed.\n")
        return 1
    finally:
        if auth is not None and token is not None:
            try:
                auth.logout(token)
            except Exception:
                err.write("Sign-out could not be confirmed; the session will expire.\n")
