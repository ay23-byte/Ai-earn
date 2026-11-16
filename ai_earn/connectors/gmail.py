"""Gmail stub — deterministic, bounded, no network, respects throttle/retry contract."""

from .base import Connector, ConnectorError, _connector_id
from .auth import validate_api_key
from .throttle import Throttle
from .normalize import normalize_gmail

class GmailConnector(Connector):
    def __init__(self, *, api_key: bytes | str, connector_id: str = "gmail-stub", throttle: Throttle | None = None, items: list[dict] | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key)
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        # deterministic stub items
        self._items = items or [
            {"id": "gmail-1", "subject": "Freelance gig: API integration", "snippet": "Need Python API help", "link": "https://mail.google.com/mail/u/0/#inbox/gmail-1", "budget_paise": 500000, "skill_tags": "python,api"},
            {"id": "gmail-2", "subject": "Design landing page", "snippet": "Figma to code", "link": "https://mail.google.com/mail/u/0/#inbox/gmail-2", "budget_paise": 300000, "skill_tags": "design,html"},
        ]
        self._revoked = False

    @property
    def connector_id(self) -> str:
        return self._id

    def validate_config(self) -> None:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        _connector_id(self._id)
        validate_api_key(self._key)

    def revoke(self):
        self._revoked = True

    def fetch(self, *, limit: int = 20, owner_id: str | None = None) -> list[dict]:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("Invalid limit.")
        # throttle check if owner provided
        if owner_id is not None:
            self._throttle.check(owner_id, self._id)
        out = []
        for raw in self._items[:limit]:
            out.append(normalize_gmail(raw))
        return out
