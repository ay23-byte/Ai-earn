"""Connector framework — auth, throttle, retry, normalize, stubs with manual fallback.

Reusable framework: each connector provides authentication handling, credential
validation, API client (BaseAPIClient), request throttling, error normalization,
retry handling, data normalization, logging (redacted), connection status, and
permission checks. Optional integrations, least-privilege scopes, never expose
tokens, manual workflow preserved.
"""

from .base import Connector, ConnectorError, AuthError, ThrottleError, PermissionError
from .throttle import Throttle
from .retry import RetryPolicy
from .auth import validate_api_key, redact_config
from .normalize import normalize_gmail, normalize_drive, normalize_github
from .status import ConnectionState, check_connection_status
from .errors import normalize_error, is_transient
from .scopes import LEAST_PRIVILEGE_SCOPES, validate_scopes_for, least_for
from .client import BaseAPIClient
from .gmail import GmailConnector
from .drive import DriveConnector
from .github import GitHubConnector
from .calendar import CalendarConnector
from .marketplace import MarketplaceConnector
from .payment import PaymentConnector
from .registry import (
    OPTIONAL_CONNECTORS,
    available_connectors,
    create_connector,
    connection_status_all,
    manual_fallback_message,
)

__all__ = [
    "Connector",
    "ConnectorError",
    "AuthError",
    "ThrottleError",
    "PermissionError",
    "Throttle",
    "RetryPolicy",
    "validate_api_key",
    "redact_config",
    "normalize_gmail",
    "normalize_drive",
    "normalize_github",
    "ConnectionState",
    "check_connection_status",
    "normalize_error",
    "is_transient",
    "LEAST_PRIVILEGE_SCOPES",
    "validate_scopes_for",
    "least_for",
    "BaseAPIClient",
    "GmailConnector",
    "DriveConnector",
    "GitHubConnector",
    "CalendarConnector",
    "MarketplaceConnector",
    "PaymentConnector",
    "OPTIONAL_CONNECTORS",
    "available_connectors",
    "create_connector",
    "connection_status_all",
    "manual_fallback_message",
]
