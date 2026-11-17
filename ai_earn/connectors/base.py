"""Base connector — least-privilege, bounded, no ToS bypass."""

import abc
import re

_CONNECTOR_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

class ConnectorError(Exception):
    pass


def _connector_id(value: str) -> str:
    if not isinstance(value, str) or _CONNECTOR_ID_RE.fullmatch(value) is None:
        raise ConnectorError("Invalid connector id.")
    if value == "manual":
        raise ConnectorError("Connector id 'manual' is reserved for manual fallback.")
    return value


class Connector(abc.ABC):
    """Abstract connector for opportunity sources.

    Implementations must:
    - not bypass ToS/robots/CAPTCHA,
    - validate config without network,
    - fetch bounded results, never trust external input.
    Manual fallback (ManualConnector in opportunities) always remains.
    """

    @property
    @abc.abstractmethod
    def connector_id(self) -> str: ...

    @abc.abstractmethod
    def fetch(self, *, limit: int = 20) -> list[dict]: ...

    def validate_config(self) -> None:
        _connector_id(self.connector_id)
