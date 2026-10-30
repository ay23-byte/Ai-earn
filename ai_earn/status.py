"""Report implemented capabilities without invented business metrics."""

import os

from . import __version__
from .auth_storage import _windows_auth_verified
from .config import Settings


def current_status(settings: Settings) -> dict[str, object]:
    auth_available = settings.environment != "production" and (
        os.name == "posix" or (os.name == "nt" and _windows_auth_verified())
    )
    return {
        "version": __version__,
        "component": "offline_foundation",
        "environment": settings.environment,
        "production_ready": False,
        "capabilities": {
            "network_listener": False,
            "persistent_storage": auth_available,
            "authentication": auth_available,
            "earning_workflows": False,
            "payments": False,
        },
    }
