"""Payment-provider connector wrapper — optional, least-privilege, sandbox by default."""

from .base import Connector, ConnectorError, _connector_id
from .auth import validate_api_key
from .throttle import Throttle
from .scopes import LEAST_PRIVILEGE_SCOPES

# Wraps ai_earn/payment_providers without exposing secrets. This stub never
# moves money; it surfaces payment order status via normalized opportunity-like
# fields for dashboard use, least-privilege payment.orders.read only.


class PaymentConnector(Connector):
    """Payment provider connector (Razorpay/Stripe stub, sandbox)."""

    def __init__(self, *, api_key: bytes | str, provider: str = "razorpay", connector_id: str = "payment-stub", throttle: Throttle | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key)
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        self._provider = provider if provider in {"razorpay", "stripe"} else "razorpay"
        self._revoked = False
        self._items = [
            {"id": "pay-1", "title": f"{self._provider.capitalize()} sandbox order #1", "html_url": f"https://dashboard.{self._provider}.com/orders/pay-1", "budget_paise": 50000, "skill_tags": f"payment,{self._provider}"},
        ]

    @property
    def connector_id(self) -> str:
        return self._id

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def scopes(self) -> list[str]:
        return list(LEAST_PRIVILEGE_SCOPES.get(self._id, LEAST_PRIVILEGE_SCOPES.get("payment-stub", ["payment.orders.read"])))

    def validate_config(self) -> None:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        _connector_id(self._id)
        validate_api_key(self._key)
        if self._provider not in {"razorpay", "stripe"}:
            raise ConnectorError("Invalid payment provider.")

    def revoke(self):
        self._revoked = True

    def connection_status(self) -> dict:
        if self._revoked:
            return {"connector_id": self._id, "status": "revoked", "connected": False, "revoked": True, "scopes": self.scopes, "provider": self._provider}
        try:
            self.validate_config()
            return {"connector_id": self._id, "status": "connected", "connected": True, "revoked": False, "scopes": self.scopes, "provider": self._provider}
        except ConnectorError as exc:
            return {"connector_id": self._id, "status": "error", "connected": False, "revoked": False, "detail": str(exc), "scopes": self.scopes}

    def check_permission(self, owner_id: str) -> None:
        super().check_permission(owner_id)

    def fetch(self, *, limit: int = 20, owner_id: str | None = None) -> list[dict]:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("Invalid limit.")
        if owner_id is not None:
            self._throttle.check(owner_id, self._id)
            self.check_permission(owner_id)
        self._log_event("fetch", f"payment fetch provider={self._provider} limit={limit}")
        # normalize via github-like shape but as opportunity
        from .normalize import normalize_github
        out = []
        for raw in self._items[:limit]:
            # map to github shape for reuse
            out.append(normalize_github({"id": raw["id"], "title": raw["title"], "html_url": raw["html_url"], "budget_paise": raw["budget_paise"], "skill_tags": raw["skill_tags"]}))
        return out
