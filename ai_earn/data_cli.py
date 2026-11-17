"""Data export and deletion CLI — owner-scoped, safeguards, never network.

Commands:
  python -m ai_earn data export   — interactive export (requires login)
  python -m ai_earn data delete   — two-phase deletion (requires password + phrase)

Both require a real terminal (hidden input). Secrets are never echoed.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from .config import ConfigurationError, load_settings, load_database_paths
from .auth import AuthService
from .auth_storage import AuthStorage
from .events import Event, write_event
from .structured_logging import get_logger
from .error_reporting import report_error

def _require_tty(inp: TextIO, err: TextIO) -> bool:
    if not inp.isatty():
        err.write("Data operations require an interactive terminal; credentials are never accepted from pipes.\n")
        return False
    return True

def _secret(prompt: str) -> str:
    # Use getpass but reject fallback that echoes
    if not sys.stdin.isatty():
        raise RuntimeError("Not a TTY")
    val = getpass.getpass(prompt)
    if not val:
        raise ValueError("Empty secret")
    return val

def run_export(environ: Mapping[str, str], inp: TextIO, out: TextIO, err: TextIO) -> int:
    logger = get_logger()
    if not _require_tty(inp, err):
        write_event(Event.INVALID_REQUEST, err)
        return 2
    try:
        load_settings(environ)
        paths = load_database_paths(environ)
    except ConfigurationError as exc:
        write_event(Event.INVALID_CONFIGURATION, err)
        err.write(f"Invalid {exc.variable}. See docs/DEPLOYMENT.md.\n")
        return 2
    try:
        out.write("User data export — owner-scoped, redacted, bounded (50 MB).\n")
        inp.write("Username: ")
        inp.flush()
        username = inp.readline().strip()
        if not username:
            err.write("Username required.\n")
            return 2
        try:
            password = _secret("Password: ")
        except Exception:
            err.write("Could not read password securely.\n")
            return 2
        auth = AuthService(AuthStorage(paths.auth, create=False))
        try:
            token = auth.login(username, password)
        except Exception as exc:
            from .error_reporting import report_error
            se = report_error(exc, event="internal_error", fields={"action": "export_login"})
            err.write(se.message + "\n")
            return 1
        # Export
        from .data_export import export_user_data

        # Destination beside state DB, timestamped
        import time

        dest = paths.state.parent / f"user-export-{username}-{int(time.time())}.json"
        result = export_user_data(token, auth_db=paths.auth, state_db=paths.state, destination=dest)
        # Logout
        try:
            auth.logout(token)
        except Exception:
            pass
        logger.info("export_succeeded", message="export ok", fields={"destination": str(dest), "total_rows": result.get("total_rows")})
        out.write(f"Export succeeded: {dest}\n")
        out.write(f"Tables: {len(result.get('tables', {}))}, rows: {result.get('total_rows')}, bytes: {result.get('estimated_bytes')}\n")
        out.write("This file is private (0o600). Do not share. Keep it outside the repo.\n")
        write_event(Event.STATUS_REQUESTED, err)
        return 0
    except Exception as exc:
        se = report_error(exc, event="export_failed")
        err.write(se.message + "\n")
        logger.error("export_failed", message=se.code)
        return 1

def run_delete(environ: Mapping[str, str], inp: TextIO, out: TextIO, err: TextIO) -> int:
    logger = get_logger()
    if not _require_tty(inp, err):
        write_event(Event.INVALID_REQUEST, err)
        return 2
    try:
        load_settings(environ)
        paths = load_database_paths(environ)
    except ConfigurationError as exc:
        write_event(Event.INVALID_CONFIGURATION, err)
        err.write(f"Invalid {exc.variable}.\n")
        return 2
    try:
        out.write("Account deletion — two-phase, requires backup and typed confirmation.\n")
        out.write("This will delete ONLY your owner rows across all tables and remove your account. Other owners are not affected.\n")
        inp.write("Username: ")
        inp.flush()
        username = inp.readline().strip()
        if not username:
            err.write("Username required.\n")
            return 2
        try:
            password = _secret("Password: ")
        except Exception:
            err.write("Could not read password.\n")
            return 2
        auth = AuthService(AuthStorage(paths.auth, create=False))
        try:
            token = auth.login(username, password)
        except Exception as exc:
            se = report_error(exc, event="internal_error", fields={"action": "delete_login"})
            err.write(se.message + "\n")
            return 1

        from .account_deletion import prepare_deletion_backup, confirm_deletion

        import time

        backup_path = paths.state.parent / f"before-delete-{username}-{int(time.time())}.sqlite3"
        try:
            prep = prepare_deletion_backup(token, auth_db=paths.auth, state_db=paths.state, backup_path=backup_path)
        except Exception as exc:
            se = report_error(exc, event="deletion_failed", fields={"phase": "prepare"})
            err.write(se.message + "\n")
            try:
                auth.logout(token)
            except Exception:
                pass
            return 1

        phrase = prep["confirmation_phrase"]
        out.write(f"Backup created: {prep['backup_path']} ({prep['backup_bytes']} bytes)\n")
        out.write(f"To confirm deletion, you must type exactly: {phrase}\n")
        inp.write(f"Type '{phrase}' to confirm: ")
        inp.flush()
        typed = inp.readline().strip()
        if typed != phrase:
            err.write("Phrase mismatch — deletion cancelled. Backup preserved.\n")
            logger.warning("deletion_cancelled", message="phrase mismatch")
            try:
                auth.logout(token)
            except Exception:
                pass
            return 1
        # Password re-entry
        try:
            password2 = _secret("Re-enter password to confirm deletion: ")
        except Exception:
            err.write("Could not read password.\n")
            try:
                auth.logout(token)
            except Exception:
                pass
            return 1
        if password2 != password:
            # Still verify via hash check in confirm_deletion, but early mismatch is ok
            pass
        try:
            result = confirm_deletion(token, password2, typed, auth_db=paths.auth, state_db=paths.state, backup_path=backup_path)
        except Exception as exc:
            se = report_error(exc, event="deletion_failed", fields={"phase": "confirm"})
            err.write(se.message + "\n")
            return 1
        out.write(f"Deletion succeeded. Removed {result['total_deleted_rows']} rows across {len(result['deleted_counts'])} tables.\n")
        out.write(f"Backup preserved at {result['backup_path']} — keep it for forensic recovery until retention expires.\n")
        out.write("Your account and all owned data have been removed from live DBs.\n")
        logger.info("deletion_succeeded", message="deletion ok", fields={"total": result['total_deleted_rows']})
        # Token is now invalid (account deleted); no logout needed
        write_event(Event.STATUS_REQUESTED, err)
        return 0
    except Exception as exc:
        se = report_error(exc, event="deletion_failed")
        err.write(se.message + "\n")
        logger.error("deletion_failed", message=se.code)
        return 1

def run(args: str, environ: Mapping[str, str], inp: TextIO, out: TextIO, err: TextIO) -> int:
    if args == "export":
        return run_export(environ, inp, out, err)
    if args == "delete":
        return run_delete(environ, inp, out, err)
    err.write("Usage: python -m ai_earn data {export|delete}\n")
    return 2
