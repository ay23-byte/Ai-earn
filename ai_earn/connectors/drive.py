"""Drive connector with deterministic default behavior and opt-in Google Drive API."""

from __future__ import annotations

import os
import urllib.parse

from .auth import OAuthFlow, validate_api_key
from .base import Connector, ConnectorError, _connector_id, _owner_id
from .errors import normalize_error
from .normalize import normalize_drive
from .scopes import LEAST_PRIVILEGE_SCOPES
from .throttle import Throttle

_LIVE_ENV = "AI_EARN_CONNECTOR_LIVE"
_MAX_LIST_LIMIT = 10_000
_PAGE_SIZE = 1_000


class DriveConnector(Connector):
    def __init__(self, *, api_key: bytes | str | None = None, connector_id: str = "drive-stub", throttle: Throttle | None = None, items: list[dict] | None = None, oauth_flow: OAuthFlow | None = None, owner_id: str | None = None) -> None:
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key) if api_key is not None else None
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        self._items = items if items is not None else [
            {"id": "drive-1", "name": "Q4 Budget Sheet – freelance", "webViewLink": "https://drive.google.com/file/d/drive-1/view", "budget_paise": 750000, "skill_tags": "sheets,finance"},
            {"id": "drive-2", "name": "Project Brief", "webViewLink": "https://drive.google.com/file/d/drive-2/view", "budget_paise": 1200000, "skill_tags": "writing,brief"},
        ]
        self._oauth = oauth_flow
        self._oauth_owner_id = _owner_id(owner_id) if owner_id is not None else None
        self._revoked = False

    @classmethod
    def from_environment(cls, *, owner_id: str, throttle: Throttle | None = None) -> "DriveConnector":
        enabled = os.environ.get(_LIVE_ENV) == "1"
        flow = OAuthFlow.from_environment(scopes=("drive.file",)) if enabled else None
        return cls(connector_id="drive" if enabled else "drive-stub", oauth_flow=flow, owner_id=owner_id, throttle=throttle)

    @property
    def connector_id(self) -> str:
        return self._id

    @property
    def scopes(self) -> list[str]:
        return list(LEAST_PRIVILEGE_SCOPES.get(self._id, LEAST_PRIVILEGE_SCOPES.get("drive-stub", ["drive.file"])))

    @property
    def live_enabled(self) -> bool:
        return os.environ.get(_LIVE_ENV) == "1"

    def validate_config(self) -> None:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        _connector_id(self._id)
        if self._key is not None:
            validate_api_key(self._key)
        if self.live_enabled:
            if self._oauth is None or self._id != "drive":
                raise ConnectorError("Live Drive mode requires configured OAuth credentials and connector_id='drive'.")
            if self._oauth_owner_id is None:
                raise ConnectorError("Live Drive mode must be bound to an authenticated owner.")
            if not self._oauth.connected:
                raise ConnectorError("Live Drive mode requires an authorized OAuth session.")

    def _bind_owner(self, owner_id: str) -> str:
        owner = _owner_id(owner_id)
        if self._oauth_owner_id is not None and self._oauth_owner_id != owner:
            raise ConnectorError("OAuth session belongs to a different owner.")
        self._oauth_owner_id = owner
        return owner

    def begin_authorization(self, *, owner_id: str) -> str:
        if self._revoked:
            raise ConnectorError("Connector revoked; create a new connector to reauthorize.")
        if not self.live_enabled or self._oauth is None:
            raise ConnectorError("OAuth authorization is available only in explicitly enabled live mode.")
        self._bind_owner(owner_id)
        return self._oauth.begin_authorization()

    def wait_for_authorization_callback(self, *, timeout: int = 180) -> tuple[str, str]:
        if not self.live_enabled or self._oauth is None:
            raise ConnectorError("OAuth authorization is available only in explicitly enabled live mode.")
        return self._oauth.wait_for_local_callback(timeout=timeout)

    def exchange_authorization_code(self, *, code: str, state: str, owner_id: str) -> None:
        if self._revoked:
            raise ConnectorError("Connector revoked; create a new connector to reauthorize.")
        if not self.live_enabled or self._oauth is None:
            raise ConnectorError("OAuth authorization is available only in explicitly enabled live mode.")
        self._bind_owner(owner_id)
        self._oauth.exchange_code(code=code, state=state)

    def revoke(self, *, owner_id: str | None = None) -> None:
        if self.live_enabled and self._oauth is not None:
            if owner_id is None or self._oauth_owner_id is None or _owner_id(owner_id) != self._oauth_owner_id:
                raise ConnectorError("OAuth session belongs to a different owner.")
        self._revoked = True
        if self._oauth is not None:
            self._oauth.revoke()

    def connection_status(self) -> dict:
        if self._revoked or (self._oauth is not None and self._oauth.revoked):
            return {"connector_id": self._id, "status": "revoked", "connected": False, "revoked": True, "scopes": self.scopes}
        try:
            if self.live_enabled and self._oauth is not None:
                if not self._oauth.connected:
                    return {"connector_id": self._id, "status": "disconnected", "connected": False, "revoked": False, "scopes": self.scopes}
                return {"connector_id": self._id, "status": "connected", "connected": True, "revoked": False, "scopes": self.scopes}
            if self.live_enabled:
                self.validate_config()
            if self._key is not None:
                validate_api_key(self._key)
            return {"connector_id": self._id, "status": "connected", "connected": True, "revoked": False, "scopes": self.scopes}
        except ConnectorError as exc:
            return {"connector_id": self._id, "status": "error", "connected": False, "revoked": False, "detail": str(normalize_error(exc)), "scopes": self.scopes}

    def check_permission(self, owner_id: str) -> None:
        super().check_permission(owner_id)

    def _check_access(self, limit: int, owner_id: str | None) -> None:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ConnectorError("Invalid limit.")
        if self.live_enabled:
            if owner_id is None or self._oauth_owner_id is None or _owner_id(owner_id) != self._oauth_owner_id:
                raise ConnectorError("Live Drive access requires its authorized owner.")
        if owner_id is not None:
            self._throttle.check(owner_id, self._id)
            self.check_permission(owner_id)
        if self.live_enabled:
            self.validate_config()

    def _list_live(self, limit: int) -> list[dict]:
        if self._oauth is None:
            raise ConnectorError("Live Drive OAuth is not configured.")
        results: list[dict] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        pages = 0
        while len(results) < limit:
            if pages >= 100:
                raise ConnectorError("Drive pagination exceeded the request limit.")
            pages += 1
            page_size = min(_PAGE_SIZE, limit - len(results))
            params = {"pageSize": page_size, "fields": "nextPageToken,files(id,name,webViewLink)", "q": "trashed=false"}
            if page_token is not None:
                params["pageToken"] = page_token
            url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(params)
            payload = self._oauth._google_api_get(url)
            files = payload.get("files", [])
            if not isinstance(files, list) or len(files) > page_size:
                raise ConnectorError("Drive API returned an invalid file list.")
            results.extend(normalize_drive(item) for item in files)
            if len(results) >= limit:
                break
            next_page_token = payload.get("nextPageToken")
            if next_page_token is None:
                break
            if not isinstance(next_page_token, str) or not 1 <= len(next_page_token) <= 4096:
                raise ConnectorError("Drive API returned an invalid page token.")
            if next_page_token in seen_page_tokens:
                raise ConnectorError("Drive API repeated a pagination marker.")
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
        return results[:limit]

    def list(self, *, limit: int = _MAX_LIST_LIMIT, owner_id: str | None = None) -> list[dict]:
        """List up to 10,000 files; live HTTP is impossible unless explicitly enabled."""
        try:
            self._check_access(limit, owner_id)
            self._log_event("fetch", f"drive list limit={limit}")
            if self.live_enabled:
                return self._list_live(limit)
            return [normalize_drive(item) for item in self._items[:limit]]
        except Exception as exc:
            raise normalize_error(exc) from exc

    def fetch(self, *, limit: int = 20, owner_id: str | None = None) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("Invalid limit.")
        return self.list(limit=limit, owner_id=owner_id)
