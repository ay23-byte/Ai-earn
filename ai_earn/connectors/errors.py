"""Error normalization — maps provider/HTTP errors to ConnectorError without leaking secrets."""

from .base import ConnectorError, AuthError, ThrottleError, PermissionError

def normalize_error(error: Exception) -> ConnectorError:
    """Map any error to a safe ConnectorError category."""
    if isinstance(error, (AuthError, ThrottleError, PermissionError, ConnectorError)):
        # Ensure message never contains token/secret
        msg = str(error)
        low = msg.lower()
        if any(k in low for k in ("api_key", "token", "secret", "bearer")):
            if "***redacted***" not in msg:
                return ConnectorError("Connector request failed.")
        return error
    msg = str(error).lower()
    if "auth" in msg or "unauthorized" in msg or "forbidden" in msg:
        return AuthError("Authentication failed; check credentials.")
    if "rate limit" in msg or "429" in msg or "quota" in msg:
        return ThrottleError("Rate limit exceeded; try later.")
    if "permission" in msg or "forbidden" in msg or "denied" in msg:
        return PermissionError("Permission denied.")
    if "invalid" in msg or "validation" in msg:
        return ConnectorError(str(error))
    return ConnectorError("Connector request failed.")


def is_transient(error: Exception) -> bool:
    """Transient errors are safe to retry (not auth/invalid/rate-limit)."""
    if isinstance(error, (AuthError,)):
        return False
    msg = str(error).lower()
    if any(k in msg for k in ("invalid", "rate limit", "auth", "permission", "revoked")):
        return False
    return isinstance(error, ConnectorError)
