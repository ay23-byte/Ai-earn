"""Report implemented capabilities without invented business metrics."""

import os

from . import __version__
from .config import Settings


def current_status(settings: Settings) -> dict[str, object]:
    return {
        "version": __version__,
        "component": "offline_foundation",
        "environment": settings.environment,
        "production_ready": False,
        "capabilities": {
            "network_listener": False,
            "persistent_storage": os.name == "posix" and settings.environment != "production",
            "authentication": os.name == "posix" and settings.environment != "production",
            "earning_workflows": False,
            "payments": False,
        },
    }
