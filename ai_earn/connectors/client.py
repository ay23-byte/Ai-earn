"""Reusable API client — throttling, retry, error normalization, logging, permission checks.

Never stores or logs credentials; all requests are bounded and owner-scoped.
Works with stub connectors (no network) and real HTTP clients alike; stubs use
in-memory items but still go through the same checks.
"""

import time
from typing import Any, Callable

from .base import ConnectorError, _owner_id
from .throttle import Throttle
from .retry import RetryPolicy
from .errors import normalize_error
from .auth import redact_config

class BaseAPIClient:
    """Generic connector client.

    Usage:
        client = BaseAPIClient(connector= GmailConnector(...), throttle=..., retry=...)
        result = client.request(owner_id="a"*32, operation="fetch", params={"limit":20})
    """

    def __init__(self, *, connector, throttle: Throttle | None = None, retry: RetryPolicy | None = None, sleeper=None, clock=None):
        self.connector = connector
        self.throttle = throttle or Throttle(limit=10, window_seconds=60)
        self.retry = retry or RetryPolicy()
        # Injectable sleeper/clock for tests — default no-sleep for fast CI, real sleep in production
        self._sleeper = sleeper  # callable(seconds) or None (skip sleep)
        self._clock = clock or time.time
        self._log: list[dict] = []

    def _log_event(self, event: str, detail: dict) -> None:
        # redact before storing
        safe = redact_config(detail)
        # Ensure no secrets leak via stringified log
        blob = str(safe).lower()
        if any(k in blob for k in ("api_key", "token", "secret")):
            if "***redacted***" not in blob:
                raise ConnectorError("Log must not contain credentials.")
        # allowlist events
        allowed = {"fetch", "validate", "throttle", "retry", "error", "status", "request"}
        if event not in allowed:
            raise ConnectorError("Invalid log event.")
        self._log.append({"event": event, "detail": safe, "at": time.time()})

    def send(self, *, owner_id: str, operation: str, params: dict | None = None, handler: Callable[..., Any] | None = None, approved: bool = False) -> Any:
        """Consequential side-effect fence (P3-2).

        ``send`` performs an externally-visible action, so it refuses to run
        without an explicit human approval signal (``approved=True``, normally
        obtained via ``ai_assistant.ApprovalQueue``). Reads use ``request``.
        """
        _owner_id(owner_id)
        if approved is not True:
            self._log_event("error", {"operation": operation, "error": "approval required"})
            raise ConnectorError("Approval required before consequential send.")
        return self.request(owner_id=owner_id, operation=operation, params=params, handler=handler)

    def request(self, *, owner_id: str, operation: str, params: dict | None = None, handler: Callable[..., Any] | None = None) -> Any:
        """Execute handler with throttling, retry, error normalization, permission checks.

        handler is called as handler(**params) inside retry loop; if None, uses
        connector.fetch(limit=...) style for backward compat.
        """
        params = params or {}
        _owner_id(owner_id)
        # permission
        try:
            self.connector.check_permission(owner_id)
        except Exception as exc:
            err = normalize_error(exc)
            self._log_event("error", {"operation": operation, "error": str(err)})
            raise err
        # throttling
        try:
            self.throttle.check(owner_id, self.connector.connector_id)
        except Exception as exc:
            err = normalize_error(exc)
            self._log_event("throttle", {"operation": operation, "error": str(err)})
            raise err
        # retry loop
        attempt = 0
        while True:
            try:
                # logging without secrets
                self._log_event("request", {"operation": operation, "params": {k: v for k, v in params.items() if k.lower() not in {"api_key","token","secret"}}})
                if handler is not None:
                    result = handler(**params)
                else:
                    # default fetch path
                    result = self.connector.fetch(**params)  # type: ignore[arg-type]
                return result
            except Exception as exc:
                err = normalize_error(exc)
                # do not log raw error if it might contain secrets
                safe_msg = str(err)
                if any(k in safe_msg.lower() for k in ("api_key", "token", "secret")) and "***redacted***" not in safe_msg:
                    safe_msg = "Connector request failed."
                    err = ConnectorError(safe_msg)
                if not self.retry.should_retry(attempt, err):
                    self._log_event("error", {"operation": operation, "error": safe_msg})
                    raise err
                # retry with backoff — use injectable sleeper to avoid long waits in tests
                delay = self.retry.delay_for(attempt)
                self._log_event("retry", {"operation": operation, "attempt": attempt, "delay": delay})
                if self._sleeper is not None:
                    try:
                        self._sleeper(delay)
                    except Exception:
                        pass
                # No real sleep when sleeper is None (fast CI); production passes time.sleep as sleeper
                attempt += 1

    def logs(self) -> list[dict]:
        # return copy without secrets (already redacted)
        return [dict(x) for x in self._log]

    def connection_status(self) -> dict:
        try:
            return self.connector.connection_status()
        except Exception as exc:
            return {"connector_id": getattr(self.connector, "connector_id", "unknown"), "status": "error", "detail": str(normalize_error(exc))}
