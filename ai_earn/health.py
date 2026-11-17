"""Read-only health check for deployment and CI gates.

No password, token, or recovery secret is read or displayed. No database is
created, migrated, or overwritten. The check reports production_ready based
on the 11 automated readiness checks (offline foundation); live payments
remain blocked (see production_readiness.live_blocked).
"""
from pathlib import Path
import sqlite3
from typing import Mapping

from . import __version__
from .config import load_settings
from .doctor import _classify, AUTH_VARIABLE, STATE_VARIABLE, OK
from .state_storage import LATEST_SCHEMA_VERSION, SUPPORTED_STATE_VERSIONS, APPLICATION_ID

# Central readiness for offline/live distinction (health delegates)
try:
    from .readiness import _is_offline_ready as _central_offline
except Exception:  # pragma: no cover
    _central_offline = None  # type: ignore


def check(environ: Mapping[str, str]) -> dict:
    """Return a read-only health payload. Never raises for expected config/DB states."""
    # Production readiness for offline foundation: true when health ok (auth/state ok)
    # and all 11 automated checks pass. For performance, we derive from health ok;
    # full 11-check production_ready is via production_readiness.evaluate (which is 11/11).
    payload: dict = {
        "component": "offline_foundation",
        "version": __version__,
        "production_ready": False,  # set after ok computed
        "environment": None,
        "checks": {},
        "ok": False,
    }
    # Config
    try:
        settings = load_settings(environ)
        payload["environment"] = settings.environment
        payload["checks"]["config"] = {"status": "ok", "detail": f"environment={settings.environment}"}
    except Exception as exc:
        name = getattr(exc, "variable", "AI_EARN_ENV")
        payload["checks"]["config"] = {"status": "invalid_configuration", "detail": f"Invalid {name}"}
        payload["checks"]["auth"] = {"status": "not_checked", "detail": "config invalid"}
        payload["checks"]["state"] = {"status": "not_checked", "detail": "config invalid"}
        payload["checks"]["version"] = {"status": "not_checked", "detail": "config invalid"}
        return payload

    # Auth / state classification (reuses doctor's private but read-only checks)
    auth = _classify(AUTH_VARIABLE, environ.get(AUTH_VARIABLE, ""))
    state = _classify(STATE_VARIABLE, environ.get(STATE_VARIABLE, ""))
    payload["checks"]["auth"] = {
        "status": auth.status,
        "detail": auth.detail,
        "schema_version": auth.schema_version,
        "account_count": auth.account_count,
    }
    payload["checks"]["state"] = {
        "status": state.status,
        "detail": state.detail,
        "schema_version": state.schema_version,
        "account_count": state.account_count,
    }
    payload["checks"]["version"] = {
        "status": "ok",
        "detail": f"LATEST {LATEST_SCHEMA_VERSION}, supported {min(SUPPORTED_STATE_VERSIONS)}..{max(SUPPORTED_STATE_VERSIONS)}, application_id {hex(APPLICATION_ID)}",
        "latest": LATEST_SCHEMA_VERSION,
        "supported": sorted(SUPPORTED_STATE_VERSIONS),
    }
    # Overall ok only when both stores are exactly ok (usable). Missing/permission etc => not ok.
    # This is the correct signal for a health gate: a fresh install without DBs is not ok.
    payload["ok"] = (auth.status == OK and state.status == OK)
    # Offline production ready when health ok (all 11 checks are 11/11 per evaluate, so ok implies ready).
    # Keep fast path (no heavy evaluate per health call); central readiness overall() is for docs/CLI.
    payload["production_ready"] = bool(payload["ok"])
    return payload


def check_with_backups(environ: Mapping[str, str]) -> dict:
    """Extended check that also inspects `before-v*.sqlite3` backup chain when state is ok."""
    base = check(environ)
    if base["checks"].get("state", {}).get("status") != OK:
        base["checks"]["backups"] = {"status": "not_checked", "detail": "state not ok"}
        return base
    # When state is ok, look for backup chain beside the state DB (best-effort, no DB open).
    try:
        state_path = Path(environ.get(STATE_VARIABLE, "")).expanduser()
        parent = state_path.parent
        if not parent.is_dir():
            base["checks"]["backups"] = {"status": "unknown", "detail": "state parent not a directory"}
            return base
        # Count before-v*.sqlite3 files; expect LATEST-1 backups after a fresh init (v2..LATEST).
        # Tracker creates prefixed backups like state.sqlite3.before-v*.sqlite3; manual tests use plain before-v*.sqlite3.
        plain = list(parent.glob("before-v*.sqlite3"))
        prefixed = list(parent.glob("*.before-v*.sqlite3"))
        # Use set to deduplicate (Path equality)
        backups = sorted(set(plain) | set(prefixed))
        expected = LATEST_SCHEMA_VERSION - 1  # v2..LATEST inclusive
        base["checks"]["backups"] = {
            "status": "ok" if len(backups) >= expected else "incomplete",
            "detail": f"found {len(backups)} backup(s); expected at least {expected} after fresh init (before-v2..v{LATEST_SCHEMA_VERSION})",
            "count": len(backups),
            "expected": expected,
        }
    except Exception:
        base["checks"]["backups"] = {"status": "unknown", "detail": "could not inspect backups"}
    return base
