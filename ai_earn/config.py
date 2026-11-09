"""Validated configuration, without including raw values in errors."""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigurationError(ValueError):
    """An invalid configuration variable, not its potentially sensitive value."""

    def __init__(self, variable: str) -> None:
        self.variable = variable
        super().__init__(f"Invalid {variable}. See .env.example for allowed values.")


@dataclass(frozen=True)
class Settings:
    environment: str
    log_level: str


def load_settings(environ: Mapping[str, str]) -> Settings:
    environment = environ.get("AI_EARN_ENV", "development")
    if environment not in {"development", "test", "production"}:
        raise ConfigurationError("AI_EARN_ENV")

    log_level = environ.get("AI_EARN_LOG_LEVEL", "INFO")
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigurationError("AI_EARN_LOG_LEVEL")

    return Settings(environment=environment, log_level=log_level)


def _database_path(value):
    # Empty is unset; a relative path is invalid. The raw value never appears
    # in error text, so misconfigured sensitive paths cannot leak into logs.
    if not value:
        return False, None
    try:
        candidate = Path(value).expanduser()
    except (OSError, RuntimeError, ValueError):
        return False, None
    if not candidate.is_absolute():
        return False, None
    return True, candidate


@dataclass(frozen=True)
class DatabasePaths:
    """Validated absolute database paths, free of any raw environment value."""

    auth: Path
    state: Path


def load_database_paths(environ):
    """Validate both required database paths before any storage is opened.

    Raises ConfigurationError naming only the offending variable. No file is
    created, opened or migrated by this function.
    """
    present, auth = _database_path(environ.get("AI_EARN_AUTH_DB", ""))
    if not present:
        raise ConfigurationError("AI_EARN_AUTH_DB")
    present, state = _database_path(environ.get("AI_EARN_STATE_DB", ""))
    if not present:
        raise ConfigurationError("AI_EARN_STATE_DB")
    return DatabasePaths(auth=auth, state=state)
