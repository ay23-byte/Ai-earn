"""Structured JSON logs — redacted, level-filtered, no secrets.

Writes one JSON object per line to the configured stream (default stderr).
Only allowlisted fields are emitted; exception text is sanitized to avoid
leaking secret values. Database paths are never logged.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TextIO

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_REDACTED = "[REDACTED]"


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# Allowlisted event names — any other string is rejected
ALLOWED_EVENTS = {
    "status_requested",
    "invalid_configuration",
    "invalid_request",
    "internal_error",
    "doctor_requested",
    "doctor_plan_requested",
    "health_check",
    "backup_started",
    "backup_succeeded",
    "backup_failed",
    "restore_started",
    "restore_succeeded",
    "restore_failed",
    "migration_started",
    "migration_succeeded",
    "migration_failed",
    "export_started",
    "export_succeeded",
    "export_failed",
    "deletion_requested",
    "deletion_confirmed",
    "deletion_cancelled",
    "deletion_failed",
    "deletion_succeeded",
    "auth_success",
    "auth_failure",
}


def _redact_value(key: str, value: Any) -> Any:
    """Redact sensitive keys without leaking value."""
    sensitive = {"password", "token", "secret", "hash", "recovery", "api_key", "webhook_secret", "db", "path"}
    lk = key.lower()
    if any(s in lk for s in sensitive):
        if isinstance(value, (str, bytes)):
            h = hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()[:8]
            return f"{_REDACTED}:sha256:{h}"
        return _REDACTED
    return value


def _sanitize_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in fields.items():
        if k in {"db_path", "auth_db", "state_db", "file"}:
            out[k] = _REDACTED
            continue
        out[k] = _redact_value(k, v)
    return out


@dataclass
class StructuredLogger:
    """JSON logger that never emits secrets or raw paths."""

    stream: TextIO = field(default_factory=lambda: sys.stderr)
    minimum_level: str = "INFO"
    service: str = "ai-earn"

    def __post_init__(self):
        if self.minimum_level not in _LEVELS:
            raise ValueError("Invalid log level")

    def _level_allows(self, level: str) -> bool:
        return _LEVELS[level] >= _LEVELS[self.minimum_level]

    def log(
        self,
        level: str,
        event: str,
        *,
        message: str | None = None,
        request_id: str | None = None,
        actor_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        if level not in _LEVELS:
            raise ValueError("Invalid level")
        if event not in ALLOWED_EVENTS:
            raise ValueError(f"Event {event} not allowlisted")
        if not self._level_allows(level):
            return
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "service": self.service,
            "event": event,
        }
        if message:
            # Never log exception repr that might contain secret
            record["message"] = message[:500]
        if request_id:
            record["request_id"] = request_id[:64]
        if actor_id:
            # Actor is opaque SHA prefix, not raw token
            record["actor_id"] = actor_id[:32]
        if fields:
            record["fields"] = _sanitize_fields({k: v for k, v in fields.items() if k})
        # Add monotonic for ordering
        record["monotonic"] = int(time.monotonic() * 1000)
        self.stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.stream.flush()

    # Convenience wrappers
    def info(self, event: str, **kw: Any) -> None:
        self.log("INFO", event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self.log("WARNING", event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self.log("ERROR", event, **kw)

    def debug(self, event: str, **kw: Any) -> None:
        self.log("DEBUG", event, **kw)


# Default process logger (stderr, INFO)
_default_logger: StructuredLogger | None = None


def get_logger(stream: TextIO | None = None, level: str | None = None) -> StructuredLogger:
    global _default_logger
    if stream is None and level is None and _default_logger is not None:
        return _default_logger
    # Resolve level from env if not given
    if level is None:
        import os

        level = os.environ.get("AI_EARN_LOG_LEVEL", "INFO")
        if level not in _LEVELS:
            level = "INFO"
    logger = StructuredLogger(stream=stream or sys.stderr, minimum_level=level)
    if stream is None and level == os.environ.get("AI_EARN_LOG_LEVEL", "INFO"):
        _default_logger = logger
    return logger
