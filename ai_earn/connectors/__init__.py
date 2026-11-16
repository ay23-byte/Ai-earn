"""Connector framework — auth, throttle, retry, normalize, stubs with manual fallback."""

from .base import Connector, ConnectorError
from .throttle import Throttle
from .retry import RetryPolicy
from .auth import validate_api_key, redact_config
from .normalize import normalize_gmail, normalize_drive, normalize_github
from .gmail import GmailConnector
from .drive import DriveConnector
from .github import GitHubConnector

__all__ = [
    "Connector",
    "ConnectorError",
    "Throttle",
    "RetryPolicy",
    "validate_api_key",
    "redact_config",
    "normalize_gmail",
    "normalize_drive",
    "normalize_github",
    "GmailConnector",
    "DriveConnector",
    "GitHubConnector",
]
