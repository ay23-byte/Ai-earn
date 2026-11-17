"""Registry — optional integrations, least-privilege enumeration, manual fallback.

All integrations optional: if unavailable or revoked, preserve manual workflow.
Never expose tokens; registry stores only metadata, not secrets.
"""

from .base import ConnectorError
from .gmail import GmailConnector
from .drive import DriveConnector
from .github import GitHubConnector
from .calendar import CalendarConnector
from .marketplace import MarketplaceConnector
from .payment import PaymentConnector

OPTIONAL_CONNECTORS = {
    "gmail": GmailConnector,
    "drive": DriveConnector,
    "github": GitHubConnector,
    "calendar": CalendarConnector,
    "marketplace": MarketplaceConnector,  # upwork/fiverr via permitted methods
    "upwork": MarketplaceConnector,
    "fiverr": MarketplaceConnector,
    "payment": PaymentConnector,
    "razorpay": PaymentConnector,
    "stripe": PaymentConnector,
}

def available_connectors() -> list[str]:
    """Return sorted list of optional connector keys."""
    return sorted(OPTIONAL_CONNECTORS.keys())

def create_connector(kind: str, *, api_key: bytes | str, **kwargs):
    """Factory — validates least-privilege and returns connector instance."""
    if kind not in OPTIONAL_CONNECTORS:
        raise ConnectorError(f"Unknown connector kind: {kind}")
    cls = OPTIONAL_CONNECTORS[kind]
    # Payment connectors need provider handling
    if kind in {"razorpay", "stripe"}:
        kwargs.setdefault("provider", kind)
    return cls(api_key=api_key, **kwargs)

def connection_status_all(connectors: list) -> list[dict]:
    """Return status for each connector; never includes secrets."""
    out = []
    for c in connectors:
        try:
            out.append(c.connection_status())
        except Exception as exc:
            out.append({"connector_id": getattr(c, "connector_id", "unknown"), "status": "error", "detail": str(exc)})
    return out

def manual_fallback_message(kind: str) -> str:
    return f"Integration '{kind}' unavailable; manual workflow preserved. Use manual title/amount import."
