"""Throttle — per-owner per-connector request cap, no external state."""

import time
import threading
from .base import ConnectorError

class Throttle:
    """Fixed-window throttle.

    limit: max requests per window_seconds per (owner_id, connector_id).
    Thread-safe for in-process use. Does not persist across restarts.
    """

    def __init__(self, limit: int = 10, window_seconds: int = 60):
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ConnectorError("Invalid throttle limit.")
        if not isinstance(window_seconds, int) or not 1 <= window_seconds <= 3600:
            raise ConnectorError("Invalid throttle window.")
        self.limit = limit
        self.window = window_seconds
        self._lock = threading.Lock()
        self._history: dict[tuple[str, str], list[float]] = {}

    def check(self, owner_id: str, connector_id: str) -> None:
        # owner_id 32 hex, connector_id 1..64
        if not isinstance(owner_id, str) or len(owner_id) != 32 or any(c not in "0123456789abcdef" for c in owner_id):
            raise ConnectorError("Invalid owner.")
        from .base import _connector_id
        _connector_id(connector_id)
        now = time.time()
        key = (owner_id, connector_id)
        with self._lock:
            lst = self._history.get(key, [])
            # prune outside window
            lst = [t for t in lst if now - t < self.window]
            if len(lst) >= self.limit:
                raise ConnectorError("Rate limit exceeded; try later.")
            lst.append(now)
            self._history[key] = lst

    def reset(self):
        with self._lock:
            self._history.clear()
