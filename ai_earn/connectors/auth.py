"""Auth helpers — bounded API key, no secrets in logs."""

from .base import ConnectorError

def validate_api_key(value: bytes | str) -> bytes:
    # Accept bytes or str, 32..128 chars/bytes, no control/whitespace tricks, no secrets logged
    if isinstance(value, str):
        # str must be 32..128, printable, no control
        if not 32 <= len(value) <= 128:
            raise ConnectorError("Invalid API key length.")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ConnectorError("Invalid API key.")
        if any(c.isspace() for c in value):
            raise ConnectorError("Invalid API key.")
        return value.encode("utf-8")
    if isinstance(value, bytes):
        if not 32 <= len(value) <= 128:
            raise ConnectorError("Invalid API key length.")
        if any(b < 32 or b == 127 for b in value):
            raise ConnectorError("Invalid API key.")
        # no whitespace bytes
        if any(chr(b).isspace() for b in value):
            raise ConnectorError("Invalid API key.")
        return value
    raise ConnectorError("Invalid API key type.")


def redact_config(config: dict) -> dict:
    """Return copy with api_key redacted for logs."""
    if not isinstance(config, dict):
        return {}
    out = {}
    for k, v in config.items():
        if k.lower() in {"api_key", "apikey", "token", "secret"}:
            out[k] = "***redacted***"
        else:
            out[k] = v
    return out
