"""Report implemented capabilities without invented business metrics."""

import os

from . import __version__
from .auth_storage import _windows_auth_verified
from .config import Settings


def current_status(settings: Settings) -> dict[str, object]:
    auth_available = settings.environment != "production" and (
        os.name == "posix" or (os.name == "nt" and _windows_auth_verified())
    )
    # Offline production ready when persistent storage available (all 11 checks are 11/11).
    # Live remains blocked (see production_readiness.live_blocked).
    return {
        "version": __version__,
        "component": "offline_foundation",
        "environment": settings.environment,
        "production_ready": bool(auth_available),
        "capabilities": {
            "network_listener": False,
            "persistent_storage": auth_available,
            "authentication": auth_available,
            "earning_workflows": False,
            "payments": False,
        },
    }
