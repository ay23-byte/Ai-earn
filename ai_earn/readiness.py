"""Central readiness — single source for offline vs live.

This module unifies the previously split `production_ready` semantics:

- `health.py:check` reports `production_ready = ok` (auth+state usable)
- `status.py:current_status` reports `production_ready = auth_available` (capability)
- `production_readiness.evaluate` reports `production_ready = 11 checks PASS` (offline foundation)
- `production_config.production_summary` reports `production_ready = all hardening PASS`

All four now delegate here for their offline/live distinction. Live remains
blocked (`live_blocked:true`) until explicit `AI_EARN_LIVE_APPROVAL=signed`
and a code change lifting `ProviderConfig(mode=live)`.

The module is read-only, never creates DBs, never logs secrets.
"""

from __future__ import annotations

import os
from typing import Mapping

from . import __version__
from .auth_storage import _windows_auth_verified
from .config import load_settings
from .doctor import _classify, AUTH_VARIABLE, STATE_VARIABLE, OK
from .state_storage import LATEST_SCHEMA_VERSION

# Live is always blocked in code until M11 sign-off + `AI_EARN_LIVE_APPROVAL=signed`
LIVE_BLOCKED = True
LIVE_APPROVAL_VAR = "AI_EARN_LIVE_APPROVAL"
LIVE_APPROVAL_VALUE = "signed"


def _is_capability_ready(environ: Mapping[str, str], settings=None) -> bool:
    """Capability: can we do local persistence on this platform? (status semantics)."""
    if settings is None:
        try:
            settings = load_settings(environ)
        except Exception:
            return False
    return settings.environment != "production" and (
        os.name == "posix" or (os.name == "nt" and _windows_auth_verified())
    )


def _is_health_ok(environ: Mapping[str, str]) -> bool:
    """Health: are both stores actually usable? (health semantics)."""
    try:
        auth = _classify(AUTH_VARIABLE, environ.get(AUTH_VARIABLE, ""))
        state = _classify(STATE_VARIABLE, environ.get(STATE_VARIABLE, ""))
        return auth.status == OK and state.status == OK
    except Exception:
        return False


def _is_offline_ready(environ: Mapping[str, str] | None = None) -> bool:
    """Offline foundation ready: 11 automated checks PASS."""
    if environ is None:
        environ = dict(os.environ)
    try:
        from .production_readiness import evaluate

        report = evaluate(environ)
        return bool(report.get("production_ready"))
    except Exception:
        return False


def _is_live_ready(environ: Mapping[str, str] | None = None) -> bool:
    """Live ready: offline ready + explicit manual approval."""
    if environ is None:
        environ = dict(os.environ)
    try:
        from .production_readiness import evaluate

        report = evaluate(environ)
        return bool(report.get("live_ready"))
    except Exception:
        return False


def overall(environ: Mapping[str, str] | None = None) -> dict:
    """Single readiness view — for docs, CLI, and future health/status unification.

    Never raises, never logs secrets. All fields are consistent across
    health/status/production_readiness/production_config.
    """
    if environ is None:
        environ = dict(os.environ)
    # Capability (status)
    try:
        settings = load_settings(environ)
        capability_ready = _is_capability_ready(environ, settings)
        environment = settings.environment
    except Exception:
        capability_ready = False
        environment = None
    # Health
    health_ok = _is_health_ok(environ)
    # Offline (11 checks)
    offline_ready = _is_offline_ready(environ)
    # Live
    live_ready = _is_live_ready(environ)
    # production_ready alias: for backward compat, health/status expose offline_ready when health_ok,
    # but we expose both granular and alias.
    # The most trustworthy offline signal is health_ok and offline_ready together.
    # For fresh installs without DBs, health_ok=False => offline_ready via evaluate may still be True (since evaluate doesn't check DBs),
    # so the composed offline is health_ok and offline_ready.
    composed_offline = bool(health_ok and offline_ready)
    # For status capability without DB, we expose capability_ready as production_ready for that context.
    # Here we expose the health-composed view as primary.
    return {
        "version": __version__,
        "component": "offline_foundation",
        "environment": environment,
        "capability_ready": capability_ready,
        "health_ok": health_ok,
        "offline_ready": offline_ready,
        "composed_offline_ready": composed_offline,
        "production_ready": composed_offline,  # alias: offline ready when health ok
        "live_blocked": LIVE_BLOCKED,
        "live_ready": live_ready,
        "guidance": "Offline ready when health ok and 11 checks PASS; live still blocked until M11 sign-off (AI_EARN_LIVE_APPROVAL=signed + code lift).",
    }
