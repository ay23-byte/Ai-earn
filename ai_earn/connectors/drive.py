"""Drive stub — deterministic, bounded, no network."""

from .base import Connector, ConnectorError, _connector_id
from .auth import validate_api_key
from .throttle import Throttle
from .normalize import normalize_drive

class DriveConnector(Connector):
    def __init__(self, *, api_key: bytes | str, connector_id: str = "drive-stub", throttle: Throttle | None = None, items: list[dict] | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._key = validate_api_key(api_key)
        self._throttle = throttle or Throttle(limit=10, window_seconds=60)
        self._items = items or [
            {"id": "drive-1", "name": "Q4 Budget Sheet – freelance", "webViewLink": "https://drive.google.com/file/d/drive-1/view", "budget_paise": 750000, "skill_tags": "sheets,finance"},
            {"id": "drive-2", "name": "Project Brief", "webViewLink": "https://drive.google.com/file/d/drive-2/view", "budget_paise": 1200000, "skill_tags": "writing,brief"},
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
        return [normalize_drive(r) for r in self._items[:limit]]
