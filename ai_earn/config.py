"""Validated configuration, without including raw values in errors."""

from dataclasses import dataclass
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
