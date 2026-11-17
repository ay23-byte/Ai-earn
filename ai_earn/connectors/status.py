"""Connection status — per-connector, owner-scoped, never exposes tokens."""

from enum import Enum

from .base import ConnectorError, _connector_id

class ConnectionState(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    REVOKED = "revoked"
    ERROR = "error"
    MANUAL = "manual"

def check_connection_status(connector) -> dict:
    """Return status dict for any connector; never includes credentials."""
    try:
        _connector_id(connector.connector_id)
        # revoked flag
        if getattr(connector, "_revoked", False):
            return {
                "connector_id": connector.connector_id,
                "status": ConnectionState.REVOKED.value,
                "connected": False,
                "revoked": True,
                "scopes": getattr(connector, "scopes", []),
            }
        # validate without network
        connector.validate_config()
        return {
            "connector_id": connector.connector_id,
            "status": ConnectionState.CONNECTED.value,
            "connected": True,
            "revoked": False,
            "scopes": getattr(connector, "scopes", []),
        }
    except ConnectorError as exc:
        return {
            "connector_id": getattr(connector, "connector_id", "unknown"),
            "status": ConnectionState.ERROR.value,
            "connected": False,
            "revoked": getattr(connector, "_revoked", False),
            "detail": str(exc),
            "scopes": getattr(connector, "scopes", []),
        }
    except Exception:
        return {
            "connector_id": getattr(connector, "connector_id", "unknown"),
            "status": ConnectionState.ERROR.value,
            "connected": False,
            "revoked": False,
            "detail": "Unknown error.",
        }
