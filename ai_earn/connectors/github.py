"""GitHub stub — deterministic, bounded, no network.

Provides 10 capabilities: auth, credential validation, API client, throttling,
error normalization, retry, data normalization, logging, connection status,
permission checks. Least-privilege repo.read, never exposes tokens.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Callable

from .base import Connector, ConnectorError, _connector_id, _owner_id
from .auth import github_api_get, validate_api_key
from .throttle import Throttle
from .normalize import normalize_github
from .scopes import LEAST_PRIVILEGE_SCOPES
from .errors import normalize_error


class GitHubConnector(Connector):
    def __init__(self, *, api_key: bytes | str | None = None, connector_id: str = "github-stub", throttle: Throttle | None = None, items: list[dict] | None = None, owner_id: str | None = None, opener: Callable | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key) if api_key is not None else None
        self._owner_id = _owner_id(owner_id) if owner_id is not None else None
        self._opener = opener
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        self._items = items or [
            {"id": "gh-1", "title": "Build API connector", "html_url": "https://github.com/example/repo/issues/1", "budget_paise": 900000, "skill_tags": "python,github", "labels": ["good first issue"]},
            {"id": "gh-2", "title": "Fix docs typo", "html_url": "https://github.com/example/repo/issues/2", "budget_paise": 50000, "skill_tags": "docs", "labels": []},
        ]
        self._revoked = False

    @property
    def connector_id(self) -> str:
        return self._id

    @property
    def scopes(self) -> list[str]:
        return list(LEAST_PRIVILEGE_SCOPES.get(self._id, LEAST_PRIVILEGE_SCOPES.get("github-stub", ["repo.read"])))

    @property
    def live_enabled(self) -> bool:
        return os.environ.get("AI_EARN_CONNECTOR_LIVE") == "1"

    @classmethod
    def from_environment(cls, *, owner_id: str, throttle: Throttle | None = None) -> "GitHubConnector":
        enabled = os.environ.get("AI_EARN_CONNECTOR_LIVE") == "1"
        token = os.environ.get("AI_EARN_GITHUB_TOKEN", "") if enabled else ""
        return cls(api_key=token or None, connector_id="github" if enabled else "github-stub", owner_id=owner_id, throttle=throttle)

    def validate_config(self) -> None:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        _connector_id(self._id)
        if self._key is not None:
            validate_api_key(self._key)
        if self.live_enabled:
            if self._key is None or self._id != "github":
                raise ConnectorError("Live GitHub mode requires a configured token and connector_id='github'.")
            if self._owner_id is None:
                raise ConnectorError("Live GitHub mode must be bound to an authenticated owner.")

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

    def _search_live(self, query: str, limit: int) -> list[dict]:
        if self._key is None:
            raise ConnectorError("Live GitHub token is not configured.")
        params = {"q": query, "per_page": min(100, limit), "page": 1}
        payload = github_api_get("https://api.github.com/search/issues?" + urllib.parse.urlencode(params), token=self._key, opener=self._opener)
        items = payload.get("items", [])
        if not isinstance(items, list) or len(items) > 100:
            raise ConnectorError("GitHub API returned an invalid item list.")
        results = []
        for entry in items[:limit]:
            if not isinstance(entry, dict):
                raise ConnectorError("GitHub API returned an invalid item.")
            title, url = entry.get("title"), entry.get("html_url")
            labels = [label.get("name") for label in entry.get("labels", []) if isinstance(label, dict) and isinstance(label.get("name"), str)]
            results.append(normalize_github({"id": str(entry.get("id", "")), "title": title, "html_url": url, "labels": labels}))
        return results

    def search(self, *, query: str = "is:issue is:open", limit: int = 20, owner_id: str | None = None) -> list[dict]:
        try:
            if not isinstance(query, str) or not 1 <= len(query) <= 256 or any(ord(c) < 32 or ord(c) == 127 for c in query):
                raise ConnectorError("Invalid GitHub search query.")
            return self._fetch(limit=limit, owner_id=owner_id, query=query)
        except Exception as exc:
            raise normalize_error(exc)

    def _fetch(self, *, limit: int, owner_id: str | None, query: str = "is:issue is:open") -> list[dict]:
        if self._revoked:
            raise ConnectorError("Connector revoked.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("Invalid limit.")
        if self.live_enabled:
            if owner_id is None or self._owner_id is None or _owner_id(owner_id) != self._owner_id:
                raise ConnectorError("Live GitHub access requires its authorized owner.")
            self.validate_config()
        if owner_id is not None:
            self._throttle.check(owner_id, self._id)
            self.check_permission(owner_id)
        self._log_event("fetch", f"github fetch limit={limit}")
        if self.live_enabled:
            return self._search_live(query, limit)
        return [normalize_github(r) for r in self._items[:limit]]

    def fetch(self, *, limit: int = 20, owner_id: str | None = None) -> list[dict]:
        try:
            return self._fetch(limit=limit, owner_id=owner_id)
        except Exception as exc:
            raise normalize_error(exc)
