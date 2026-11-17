"""Calendar stub — deterministic, bounded, no network, least-privilege."""

from .base import Connector, ConnectorError, _connector_id
from .auth import validate_api_key
from .throttle import Throttle
from .normalize import normalize_gmail  # reuse similar normalization pattern but map to same opportunity fields
from .scopes import LEAST_PRIVILEGE_SCOPES

def _normalize_calendar(raw: dict) -> dict:
    # Calendar events as opportunities: deadline/follow-up source
    if not isinstance(raw, dict):
        raise ConnectorError("Invalid Calendar item.")
    allowed = {"id", "summary", "htmlLink", "budget_paise", "deadline_at", "risk_level", "skill_tags"}
    if not set(raw).issubset(allowed):
        raise ConnectorError("Extra field in Calendar item.")
    if "summary" not in raw or "htmlLink" not in raw:
        raise ConnectorError("Missing Calendar field.")
    # reuse validation via helper
    from .normalize import _title, _url, _budget
    title = _title(raw["summary"])
    url = _url(raw["htmlLink"])
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
    if any(ord(c) < 32 or ord(c) == 127 for c in tags):
        raise ConnectorError("Invalid skill_tags.")
    return {"title": title, "source_url": url, "budget_paise": budget, "deadline_at": deadline, "risk_level": risk, "skill_tags": tags}


class CalendarConnector(Connector):
    """Calendar connector — least-privilege calendar.readonly, manual fallback if unavailable."""

    def __init__(self, *, api_key: bytes | str, connector_id: str = "calendar-stub", throttle: Throttle | None = None, items: list[dict] | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key)
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        self._items = items or [
            {"id": "cal-1", "summary": "Client call: follow-up proposal", "htmlLink": "https://calendar.google.com/calendar/event?eid=cal-1", "budget_paise": 200000, "skill_tags": "communication,meeting"},
            {"id": "cal-2", "summary": "Deadline: deliver draft", "htmlLink": "https://calendar.google.com/calendar/event?eid=cal-2", "budget_paise": 0, "skill_tags": "deadline,delivery"},
        ]
        self._revoked = False

    @property
    def connector_id(self) -> str:
        return self._id

    @property
    def scopes(self) -> list[str]:
        return list(LEAST_PRIVILEGE_SCOPES.get(self._id, LEAST_PRIVILEGE_SCOPES.get("calendar-stub", ["calendar.readonly"])))

    def validate_config(self) -> None:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        _connector_id(self._id)
        validate_api_key(self._key)
        # scopes validated via base

    def revoke(self):
        self._revoked = True

    def connection_status(self) -> dict:
        if self._revoked:
            return {"connector_id": self._id, "status": "revoked", "connected": False, "revoked": True, "scopes": self.scopes}
        try:
            self.validate_config()
            return {"connector_id": self._id, "status": "connected", "connected": True, "revoked": False, "scopes": self.scopes}
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
        # logging redacted (no secrets)
        self._log_event("fetch", f"calendar fetch limit={limit}")
        return [_normalize_calendar(r) for r in self._items[:limit]]
