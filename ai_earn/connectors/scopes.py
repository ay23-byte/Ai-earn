"""Least-privilege scopes — each integration declares minimal required scopes."""

from .base import ConnectorError, _connector_id, _validate_scopes

LEAST_PRIVILEGE_SCOPES: dict[str, list[str]] = {
    "gmail": ["gmail.readonly"],
    "gmail-stub": ["gmail.readonly"],
    "drive": ["drive.file"],
    "drive-stub": ["drive.file"],
    "github": ["repo.read"],
    "github-stub": ["repo.read"],
    "calendar": ["calendar.readonly"],
    "calendar-stub": ["calendar.readonly"],
    "upwork": ["opportunity.read"],
    "fiverr": ["opportunity.read"],
    "marketplace-stub": ["opportunity.read"],
    "payment": ["payment.orders.read"],
    "payment-stub": ["payment.orders.read"],
    "razorpay": ["payment.orders.read"],
    "stripe": ["payment.orders.read"],
    "manual": [],
    "manual-fallback": [],
    "fake-test-connector": [],
}

def validate_scopes_for(connector_id: str, scopes: list[str]) -> list[str]:
    _connector_id(connector_id)
    _validate_scopes(scopes)
    least = LEAST_PRIVILEGE_SCOPES.get(connector_id, None)
    if least is not None:
        # reject requesting more than least-privilege
        extra = set(scopes) - set(least)
        if extra:
            raise ConnectorError(f"Scope exceeds least-privilege for {connector_id}: {extra}")
    return scopes

def least_for(connector_id: str) -> list[str]:
    return list(LEAST_PRIVILEGE_SCOPES.get(connector_id, []))
