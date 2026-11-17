"""GitHub stub — deterministic, bounded, no network."""

from .base import Connector, ConnectorError, _connector_id
from .auth import validate_api_key
from .throttle import Throttle
from .normalize import normalize_github

class GitHubConnector(Connector):
    def __init__(self, *, api_key: bytes | str, connector_id: str = "github-stub", throttle: Throttle | None = None, items: list[dict] | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key)
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        self._items = items or [
            {"id": "gh-1", "title": "Build API connector", "html_url": "https://github.com/example/repo/issues/1", "budget_paise": 900000, "skill_tags": "python,github", "labels": ["good first issue"]},
            {"id": "gh-2", "title": "Fix docs typo", "html_url": "https://github.com/example/repo/issues/2", "budget_paise": 50000, "skill_tags": "docs", "labels": []},
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
        if owner_id is not None:
            self._throttle.check(owner_id, self._id)
        return [normalize_github(r) for r in self._items[:limit]]
