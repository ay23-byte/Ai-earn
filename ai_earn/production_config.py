"""Production configuration hardening — validates production env without leaking secrets.

Never exposes the SQLite database over the network. Production is local-offline
first; any future HTTP layer must bind 127.0.0.1, never 0.0.0.0, and must not
serve *.sqlite3 files. Database paths must be owner-only and never under a web root.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .config import ConfigurationError, load_settings, load_database_paths

# Production allowlist — change requires security review
PRODUCTION_ALLOWED_ENV = "production"
PRODUCTION_REQUIRED_VARS = ("AI_EARN_AUTH_DB", "AI_EARN_STATE_DB")
# Secrets must be env-only, never committed, length validated per service
WEBHOOK_SECRET_MIN = 32
WEBHOOK_SECRET_MAX = 128


@dataclass(frozen=True)
class ProductionCheck:
    id: str
    passed: bool
    detail: str


def _is_private_db_path(path: Path) -> bool:
    """Check path is absolute and parent is private without leaking value."""
    if not path.is_absolute():
        return False
    # Parent privacy is checked via existing storage gates; here we just ensure
    # path does not look like web-exposed (never under dashboard/, /var/www, etc.)
    # and never binds 0.0.0.0 / network.
    p = str(path).lower()
    forbidden_substrings = ("dashboard", "www", "htdocs", "public", "static")
    # Allow dashboard-snapshot beside private dir, but never DB under dashboard
    if any(x in p for x in forbidden_substrings):
        return False
    return True


def _check_no_network_db_exposure(environ: Mapping[str, str]) -> ProductionCheck:
    """Ensure DB is never exposed via network listeners."""
    # We scan environ for any var that looks like binding to 0.0.0.0 with DB path
    # and verify that dashboard server is documented to bind 127.0.0.1 only.
    # Code-level guard: never start HTTP server on 0.0.0.0 with DB files in web root.
    # This check is documentary; runtime check verifies DB parent is not web-accessible.
    try:
        paths = load_database_paths(environ)
        # If DB paths are under a temp web root, fail
        if not _is_private_db_path(paths.auth) or not _is_private_db_path(paths.state):
            return ProductionCheck(
                "no_network_db_exposure",
                False,
                "Database must reside in a private owner-only directory, never under a web root (dashboard/www). "
                "Bind any HTTP server to 127.0.0.1 only; never 0.0.0.0.",
            )
        # Ensure no 0.0.0.0 binding is configured via env
        bind = environ.get("AI_EARN_BIND", "127.0.0.1")
        if bind == "0.0.0.0":
            return ProductionCheck(
                "no_network_db_exposure",
                False,
                "Refusing to bind 0.0.0.0 with local SQLite — database would be network-exposed. Use 127.0.0.1 only.",
            )
        return ProductionCheck(
            "no_network_db_exposure",
            True,
            "Database is private and not network-exposed; any HTTP must bind 127.0.0.1.",
        )
    except ConfigurationError as exc:
        return ProductionCheck("no_network_db_exposure", False, f"Invalid {exc.variable}")


def validate_production(environ: Mapping[str, str]) -> list[ProductionCheck]:
    """Validate production hardening. Never raises with secret values."""
    checks: list[ProductionCheck] = []
    # 1. Environment must be explicitly production
    try:
        settings = load_settings(environ)
        if settings.environment != PRODUCTION_ALLOWED_ENV:
            checks.append(
                ProductionCheck(
                    "environment",
                    False,
                    f"Environment is {settings.environment}, not production. Set AI_EARN_ENV=production only after readiness review.",
                )
            )
        else:
            checks.append(ProductionCheck("environment", True, "Environment is production."))
    except ConfigurationError as exc:
        checks.append(ProductionCheck("environment", False, f"Invalid {exc.variable}"))
        return checks

    # 2. Database paths present, absolute, private, not web-exposed
    try:
        paths = load_database_paths(environ)
        checks.append(ProductionCheck("database_paths", True, "AI_EARN_AUTH_DB and AI_EARN_STATE_DB are absolute paths."))
        # Private directory checks are done by storage gates; we re-check exposure
        checks.append(_check_no_network_db_exposure(environ))
        # Auth and state must be distinct files in same private directory
        if paths.auth == paths.state:
            checks.append(ProductionCheck("distinct_dbs", False, "Auth and state must be distinct files."))
        else:
            checks.append(ProductionCheck("distinct_dbs", True, "Auth and state are distinct."))
        if paths.auth.parent != paths.state.parent:
            checks.append(
                ProductionCheck(
                    "same_private_parent",
                    False,
                    "Auth and state must share the same private directory for backup atomicity.",
                )
            )
        else:
            checks.append(ProductionCheck("same_private_parent", True, "Auth and state share private parent."))
    except ConfigurationError as exc:
        checks.append(ProductionCheck("database_paths", False, f"Invalid {exc.variable}"))
        return checks

    # 3. Log level not DEBUG in production (avoid verbose secret leakage)
    level = environ.get("AI_EARN_LOG_LEVEL", "INFO")
    if level == "DEBUG":
        checks.append(
            ProductionCheck(
                "log_level",
                False,
                "Production must not use DEBUG level — would increase log volume and risk secret verbosity. Use INFO/WARNING/ERROR.",
            )
        )
    else:
        checks.append(ProductionCheck("log_level", True, f"Log level {level} is production-appropriate."))

    # 4. No .env auto-load (must be env-only)
    if Path(".env").exists() or Path("dashboard/.env").exists():
        checks.append(
            ProductionCheck(
                "no_dotenv",
                False,
                ".env file found — production must use process environment only; do not commit secrets. Remove .env and use OS env.",
            )
        )
    else:
        checks.append(ProductionCheck("no_dotenv", True, "No .env auto-load; secrets are env-only."))

    # 5. Production readiness remains false until manual sign-off
    checks.append(
        ProductionCheck(
            "production_ready_gate",
            False,
            "production_ready is always false in local SQLite build. Do not flip without independent security, recovery, and legal reviews (see docs/PRODUCTION_READINESS.md).",
        )
    )

    return checks


def production_summary(environ: Mapping[str, str] | None = None) -> dict:
    """Return JSON-serializable hardening summary. Never includes secret values."""
    if environ is None:
        environ = dict(os.environ)
    checks = validate_production(environ)
    return {
        "production_hardened": all(c.passed for c in checks if c.id != "production_ready_gate"),
        "production_ready": False,
        "checks": [{"id": c.id, "passed": c.passed, "detail": c.detail} for c in checks],
        "guidance": "Local SQLite is not network-exposed. Bind any HTTP to 127.0.0.1, never 0.0.0.0. See docs/DEPLOYMENT.md (Windows local first) and docs/PRODUCTION_READINESS.md (11-item gate).",
        "note": "Never expose *.sqlite3, *.sqlite3-wal, or dashboard-snapshot-*.json over the network.",
    }
