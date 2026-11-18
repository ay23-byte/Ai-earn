"""Gmail connector with deterministic default behavior and opt-in Gmail API.

Stub mode is the default and performs no network access. Live mode requires
``AI_EARN_CONNECTOR_LIVE=1``, an owner-bound ``OAuthFlow`` authorized with the
least-privilege ``gmail.readonly`` scope, and the same owner on every call.
Live reads are metadata/snippet only (``format=metadata``, Subject header);
bodies are never requested and messages are never modified.
"""

from __future__ import annotations

import os
import urllib.parse

from .auth import OAuthFlow, validate_api_key
from .base import Connector, ConnectorError, _connector_id, _owner_id
from .errors import normalize_error
from .normalize import normalize_gmail
from .scopes import LEAST_PRIVILEGE_SCOPES
from .throttle import Throttle

_LIVE_ENV = "AI_EARN_CONNECTOR_LIVE"
_MESSAGES_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


class GmailConnector(Connector):
    def __init__(self, *, api_key: bytes | str | None = None, connector_id: str = "gmail-stub", throttle: Throttle | None = None, items: list[dict] | None = None, oauth_flow: OAuthFlow | None = None, owner_id: str | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key) if api_key is not None else None
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        self._items = items if items is not None else [
            {"id": "gmail-1", "subject": "Freelance gig: API integration", "snippet": "Need Python API help", "link": "https://mail.google.com/mail/u/0/#inbox/gmail-1", "budget_paise": 500000, "skill_tags": "python,api"},
            {"id": "gmail-2", "subject": "Design landing page", "snippet": "Figma to code", "link": "https://mail.google.com/mail/u/0/#inbox/gmail-2", "budget_paise": 300000, "skill_tags": "design,html"},
        ]
        self._oauth = oauth_flow
        self._oauth_owner_id = _owner_id(owner_id) if owner_id is not None else None
        self._revoked = False

    @classmethod
    def from_environment(cls, *, owner_id: str, throttle: Throttle | None = None) -> "GmailConnector":
        enabled = os.environ.get(_LIVE_ENV) == "1"
        flow = OAuthFlow.from_environment(scopes=("gmail.readonly",)) if enabled else None
        return cls(connector_id="gmail" if enabled else "gmail-stub", oauth_flow=flow, owner_id=owner_id, throttle=throttle)

    @property
    def connector_id(self) -> str:
        return self._id

    @property
    def scopes(self) -> list[str]:
        return list(LEAST_PRIVILEGE_SCOPES.get(self._id, LEAST_PRIVILEGE_SCOPES.get("gmail-stub", ["gmail.readonly"])))

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
            if self._oauth is None or self._id != "gmail":
                raise ConnectorError("Live Gmail mode requires configured OAuth credentials and connector_id='gmail'.")
            if self._oauth_owner_id is None:
                raise ConnectorError("Live Gmail mode must be bound to an authenticated owner.")
            if not self._oauth.connected:
                raise ConnectorError("Live Gmail mode requires an authorized OAuth session.")

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

    def revoke(self, *, owner_id: str | None = None):
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
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("Invalid limit.")
        if self.live_enabled:
            if owner_id is None or self._oauth_owner_id is None or _owner_id(owner_id) != self._oauth_owner_id:
                raise ConnectorError("Live Gmail access requires its authorized owner.")
        if owner_id is not None:
            self._throttle.check(owner_id, self._id)
            self.check_permission(owner_id)
        if self.live_enabled:
            self.validate_config()

    @staticmethod
    def _validate_query(query: str) -> str:
        if not isinstance(query, str) or len(query) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in query):
            raise ConnectorError("Invalid Gmail search query.")
        return query

    def _search_live(self, query: str, limit: int) -> list[dict]:
        if self._oauth is None:
            raise ConnectorError("Live Gmail OAuth is not configured.")
        results: list[dict] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        pages = 0
        while len(results) < limit:
            if pages >= 10:
                raise ConnectorError("Gmail pagination exceeded the request limit.")
            pages += 1
            params: dict[str, object] = {"maxResults": min(100, limit - len(results))}
            if query:
                params["q"] = query
            if page_token is not None:
                params["pageToken"] = page_token
            payload = self._oauth._google_api_get(_MESSAGES_ENDPOINT + "?" + urllib.parse.urlencode(params))
            messages = payload.get("messages", [])
            if not isinstance(messages, list) or len(messages) > 100:
                raise ConnectorError("Gmail API returned an invalid message list.")
            for entry in messages:
                if len(results) >= limit:
                    break
                if not isinstance(entry, dict):
                    raise ConnectorError("Gmail API returned an invalid message entry.")
                message_id = entry.get("id")
                if not isinstance(message_id, str) or not 1 <= len(message_id) <= 128 or not all(c.isalnum() or c in "_-" for c in message_id):
                    raise ConnectorError("Gmail API returned an invalid message id.")
                detail = self._oauth._google_api_get(
                    _MESSAGES_ENDPOINT + "/" + urllib.parse.quote(message_id, safe="") + "?" + urllib.parse.urlencode({"format": "metadata", "metadataHeaders": "Subject"})
                )
                results.append(normalize_gmail(self._to_stub_item(message_id, detail)))
            if len(results) >= limit:
                break
            next_page_token = payload.get("nextPageToken")
            if next_page_token is None:
                break
            if not isinstance(next_page_token, str) or not 1 <= len(next_page_token) <= 4096:
                raise ConnectorError("Gmail API returned an invalid page token.")
            if next_page_token in seen_page_tokens:
                raise ConnectorError("Gmail API repeated a pagination marker.")
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token
        return results[:limit]

    @staticmethod
    def _to_stub_item(message_id: str, detail: dict) -> dict:
        if not isinstance(detail, dict) or detail.get("id") != message_id:
            raise ConnectorError("Gmail API returned a mismatched message detail.")
        subject = "(no subject)"
        payload = detail.get("payload")
        if isinstance(payload, dict):
            headers = payload.get("headers")
            if isinstance(headers, list):
                for header in headers:
                    if isinstance(header, dict) and str(header.get("name", "")).lower() == "subject" and isinstance(header.get("value"), str) and header["value"].strip():
                        subject = header["value"].strip()[:200]
                        break
        snippet = detail.get("snippet", "")
        if not isinstance(snippet, str):
            raise ConnectorError("Gmail API returned an invalid snippet.")
        return {"id": message_id, "subject": subject, "snippet": snippet[:2000], "link": f"https://mail.google.com/mail/u/0/#inbox/{message_id}"}

    def search(self, *, query: str = "", limit: int = 20, owner_id: str | None = None) -> list[dict]:
        try:
            query = self._validate_query(query)
            self._check_access(limit, owner_id)
            self._log_event("fetch", f"gmail search limit={limit}")
            if self.live_enabled:
                return self._search_live(query, limit)
            needle = query.lower()
            matched = [item for item in self._items if not needle or needle in str(item.get("subject", "")).lower() or needle in str(item.get("snippet", "")).lower()]
            return [normalize_gmail(raw) for raw in matched[:limit]]
        except Exception as exc:
            raise normalize_error(exc) from exc

    def fetch(self, *, limit: int = 20, owner_id: str | None = None) -> list[dict]:
        return self.search(query="", limit=limit, owner_id=owner_id)
