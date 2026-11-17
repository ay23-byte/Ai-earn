"""Error reporting — sanitized, structured, never leaks secrets or paths.

Each error gets a short error_id (8 hex) for correlation. The returned
user message is safe to display; the structured log contains redacted fields.
"""

from __future__ import annotations

import hashlib
import secrets
import traceback
from dataclasses import dataclass
from typing import Any

from .structured_logging import StructuredLogger, get_logger


@dataclass(frozen=True)
class SafeError:
    error_id: str
    code: str
    message: str  # safe for user
    http_status: int = 500


# Map internal exceptions to safe codes — never expose raw DB path or secret
_CODE_MAP: dict[str, tuple[str, int]] = {
    "ConfigurationError": ("invalid_configuration", 400),
    "StorageError": ("storage_unavailable", 503),
    "StateStorageError": ("state_unavailable", 503),
    "AccessDenied": ("access_denied", 403),
    "AuthError": ("authentication_failed", 401),
    "RateLimitError": ("rate_limited", 429),
    "ValueError": ("invalid_input", 400),
    "FileNotFoundError": ("not_found", 404),
}


def _new_error_id() -> str:
    return secrets.token_hex(4)  # 8 chars


def _safe_code(exc: BaseException) -> tuple[str, int]:
    name = type(exc).__name__
    return _CODE_MAP.get(name, ("internal_error", 500))


def report_error(
    exc: BaseException,
    *,
    event: str = "internal_error",
    fields: dict[str, Any] | None = None,
    logger: StructuredLogger | None = None,
) -> SafeError:
    """Log redacted error and return safe user-facing error. Never re-raises secret."""
    if logger is None:
        logger = get_logger()
    error_id = _new_error_id()
    code, status = _safe_code(exc)
    # Hash exception type for log correlation, not message
    exc_hash = hashlib.sha256(f"{type(exc).__name__}".encode()).hexdigest()[:8]
    safe_fields = dict(fields or {})
    safe_fields["error_id"] = error_id
    safe_fields["exception_hash"] = exc_hash
    # Never log exc args that might contain secret/path
    logger.error(
        event,
        message=f"{code} id={error_id}",
        fields=safe_fields,
    )
    # User message: never include path/secret
    user_msg = {
        "invalid_configuration": "Configuration is invalid. Check AI_EARN_ENV and database paths (see docs/DEPLOYMENT.md).",
        "storage_unavailable": "Storage is unavailable. Run python -m ai_earn doctor --json for diagnosis.",
        "state_unavailable": "State store is unavailable. Run python -m ai_earn doctor --json.",
        "access_denied": "Access denied. Check credentials and session expiry.",
        "authentication_failed": "Authentication failed.",
        "rate_limited": "Too many attempts. Wait 15 minutes.",
        "invalid_input": "Input is invalid.",
        "not_found": "Resource not found.",
        "internal_error": "An unexpected error occurred. Use error_id to correlate with logs.",
    }.get(code, "An error occurred.")
    return SafeError(error_id=error_id, code=code, message=f"{user_msg} (id={error_id})", http_status=status)


def format_safe_error(err: SafeError) -> dict[str, Any]:
    return {"error_id": err.error_id, "code": err.code, "message": err.message, "status": err.http_status}
