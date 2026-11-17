"""Marketplace stub — Upwork/Fiverr via permitted integration methods only, bounded, manual fallback."""

from .base import Connector, ConnectorError, _connector_id
from .auth import validate_api_key
from .throttle import Throttle
from .scopes import LEAST_PRIVILEGE_SCOPES

def _normalize_marketplace(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConnectorError("Invalid Marketplace item.")
    allowed = {"id", "title", "url", "budget_paise", "deadline_at", "risk_level", "skill_tags", "platform"}
    if not set(raw).issubset(allowed):
        raise ConnectorError("Extra field in Marketplace item.")
    if "title" not in raw or "url" not in raw:
        raise ConnectorError("Missing Marketplace field.")
    from .normalize import _title, _url, _budget
    title = _title(raw["title"])
    url = _url(raw["url"])
    budget = _budget(raw.get("budget_paise", 0))
    deadline = raw.get("deadline_at", 0)
    if not isinstance(deadline, int) or not 0 <= deadline <= 4102444800:
        raise ConnectorError("Invalid deadline.")
    risk = raw.get("risk_level", "unknown")
    if risk not in {"low", "medium", "high", "unknown"}:
        raise ConnectorError("Invalid risk.")
    tags = raw.get("skill_tags", "")
    if not isinstance(tags, str) or len(tags) > 500:
        raise ConnectorError("Invalid skill_tags.")
    platform = raw.get("platform", "upwork")
    if platform not in {"upwork", "fiverr", "freelance"}:
        raise ConnectorError("Invalid platform.")
    return {"title": title, "source_url": url, "budget_paise": budget, "deadline_at": deadline, "risk_level": risk, "skill_tags": tags, "platform": platform}


class MarketplaceConnector(Connector):
    """Upwork/Fiverr via permitted methods only — never bypass ToS/robots/CAPTCHA. Manual import always remains."""

    def __init__(self, *, api_key: bytes | str, connector_id: str = "marketplace-stub", platform: str = "upwork", throttle: Throttle | None = None, items: list[dict] | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key)
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        self._platform = platform if platform in {"upwork", "fiverr"} else "upwork"
        self._items = items or [
            {"id": "mp-1", "title": "Upwork: Build MVP landing page", "url": "https://www.upwork.com/jobs/~mp-1", "budget_paise": 1500000, "skill_tags": "web,landing", "platform": "upwork"},
            {"id": "mp-2", "title": "Fiverr: Logo design gig", "url": "https://www.fiverr.com/gig/mp-2", "budget_paise": 250000, "skill_tags": "design,logo", "platform": "fiverr"},
        ]
        self._revoked = False

    @property
    def connector_id(self) -> str:
        return self._id

    @property
    def scopes(self) -> list[str]:
        return list(LEAST_PRIVILEGE_SCOPES.get(self._id, LEAST_PRIVILEGE_SCOPES.get("marketplace-stub", ["opportunity.read"])))

    def validate_config(self) -> None:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        _connector_id(self._id)
        validate_api_key(self._key)

    def revoke(self):
        self._revoked = True

    def connection_status(self) -> dict:
        if self._revoked:
            return {"connector_id": self._id, "status": "revoked", "connected": False, "revoked": True, "scopes": self.scopes}
        try:
            self.validate_config()
            return {"connector_id": self._id, "status": "connected", "connected": True, "revoked": False, "scopes": self.scopes, "platform": self._platform}
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
        self._log_event("fetch", f"marketplace fetch platform={self._platform} limit={limit}")
        out = []
        for raw in self._items[:limit]:
            # ensure permitted platform handling: never bypass ToS, only stub data
            out.append(_normalize_marketplace(raw))
        # strip platform from normalized output for opportunity compatibility (keep only standard fields)
        # Keep platform internally but not required for opportunity import; map to opportunity fields
        return [{k: v for k, v in x.items() if k != "platform"} for x in out]
