"""Structured operational events, using a closed allowlist instead of raw messages.

Do not pass arbitrary objects or exceptions to logs: they can contain credentials.
"""

from datetime import datetime, timezone
from enum import Enum
import json
from typing import TextIO


class Event(Enum):
    STATUS_REQUESTED = ("status_requested", "INFO")
    INVALID_REQUEST = ("invalid_request", "WARNING")
    INVALID_CONFIGURATION = ("invalid_configuration", "ERROR")
    INTERNAL_ERROR = ("internal_error", "ERROR")


_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def write_event(event: Event, stream: TextIO, minimum_level: str = "INFO") -> None:
    if not isinstance(event, Event):
        raise TypeError("Event must be an allowlisted Event")
    if minimum_level not in _LEVELS:
        raise ValueError("Invalid minimum log level")
    name, level = event.value
    if _LEVELS[level] < _LEVELS[minimum_level]:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": name,
    }
    stream.write(json.dumps(record, separators=(",", ":")) + "\n")
