"""Gmail stub — deterministic, bounded, no network, respects throttle/retry contract.

Provides 10 capabilities: auth, credential validation, API client, throttling,
error normalization, retry, data normalization, logging, connection status,
permission checks. Least-privilege gmail.readonly, never exposes tokens.
"""

from .base import Connector, ConnectorError, _connector_id
from .auth import validate_api_key
from .throttle import Throttle
from .normalize import normalize_gmail
from .scopes import LEAST_PRIVILEGE_SCOPES
from .errors import normalize_error


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

    @property
    def scopes(self) -> list[str]:
        return list(LEAST_PRIVILEGE_SCOPES.get(self._id, LEAST_PRIVILEGE_SCOPES.get("gmail-stub", ["gmail.readonly"])))

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
            return {"connector_id": self._id, "status": "connected", "connected": True, "revoked": False, "scopes": self.scopes}
        except ConnectorError as exc:
            return {"connector_id": self._id, "status": "error", "connected": False, "revoked": False, "detail": str(normalize_error(exc)), "scopes": self.scopes}

    def check_permission(self, owner_id: str) -> None:
        super().check_permission(owner_id)

    def fetch(self, *, limit: int = 20, owner_id: str | None = None) -> list[dict]:
        try:
            if self._revoked:
                raise ConnectorError("Connector revoked.")
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ConnectorError("Invalid limit.")
            if owner_id is not None:
                self._throttle.check(owner_id, self._id)
                self.check_permission(owner_id)
            self._log_event("fetch", f"gmail fetch limit={limit}")
            out = []
            for raw in self._items[:limit]:
                out.append(normalize_gmail(raw))
            return out
        except Exception as exc:
            raise normalize_error(exc)
