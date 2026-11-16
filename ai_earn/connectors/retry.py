"""Retry policy — bounded retries with backoff, no unbounded loops."""

from .base import ConnectorError

class RetryPolicy:
    def __init__(self, max_retries: int = 3, base_delay: float = 0.1, max_delay: float = 5.0):
        if not isinstance(max_retries, int) or not 0 <= max_retries <= 5:
            raise ConnectorError("Invalid max_retries.")
        if not isinstance(base_delay, (int, float)) or not 0 <= base_delay <= 5:
            raise ConnectorError("Invalid base_delay.")
        if not isinstance(max_delay, (int, float)) or not base_delay <= max_delay <= 60:
            raise ConnectorError("Invalid max_delay.")
        self.max_retries = max_retries
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)

    def delay_for(self, attempt: int) -> float:
        if attempt < 0 or attempt > self.max_retries:
            raise ConnectorError("Invalid attempt.")
        # exponential backoff capped
        delay = self.base_delay * (2 ** attempt)
        return min(delay, self.max_delay)

    def should_retry(self, attempt: int, error: Exception) -> bool:
        # Only retry on transient ConnectorError with message containing retryable?
        # For stub, we retry on any ConnectorError except auth/validation
        if attempt >= self.max_retries:
            return False
        msg = str(error).lower()
        if "invalid" in msg or "rate limit" in msg or "auth" in msg:
            return False
        return isinstance(error, ConnectorError)
