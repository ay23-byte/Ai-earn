"""Secure secret management — generation, validation, and redaction.

Secrets are process-environment only, never committed, never logged, never
auto-loaded from .env. All helpers redact values in errors and logs.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from pathlib import Path

# Allowed secret patterns — no private keys in repo
SECRET_MIN_BYTES = 32
SECRET_MAX_BYTES = 128
# CI scan patterns (must not appear in repo) — constructed to avoid self-match in grep
_FORB_BP = "BEGIN" + " PRIVATE KEY"
FORBIDDEN_PATTERNS = [
    _FORB_BP,
    r"sk-[A-Za-z0-9]{20}",
    r"AKIA[0-9A-Z]{16}",
]


def generate_secret(num_bytes: int = 32) -> str:
    """Generate a urlsafe secret for env use. Does not write to disk."""
    if not (SECRET_MIN_BYTES <= num_bytes <= SECRET_MAX_BYTES):
        raise ValueError("Secret length must be 32..128 bytes before encoding")
    return secrets.token_urlsafe(num_bytes)


def validate_secret(name: str, value: str | bytes) -> None:
    """Validate secret length and charset without logging value."""
    if not isinstance(name, str) or not name:
        raise ValueError("Secret name required")
    # Normalize to bytes length
    if isinstance(value, str):
        # urlsafe base64 or hex; check decoded length roughly
        raw = value.encode()
        # Accept 32..128 bytes when decoded or direct
        # For string secrets, require 32..128 characters and high entropy
        if not (32 <= len(value) <= 256):
            raise ValueError(f"Invalid {name}: length must be 32..256")
        if len(value.strip()) != len(value) or " " in value:
            raise ValueError(f"Invalid {name}: whitespace not allowed")
    else:
        if not (SECRET_MIN_BYTES <= len(value) <= SECRET_MAX_BYTES):
            raise ValueError(f"Invalid {name}: byte length must be 32..128")
    # Reject low-entropy examples
    if isinstance(value, str) and re.fullmatch(r"(.)\1+", value):
        raise ValueError(f"Invalid {name}: low entropy")


def redact(value: str | bytes | None) -> str:
    """Return redacted placeholder; never returns original."""
    if value is None or value == "":
        return "[REDACTED:empty]"
    # Return hash prefix for correlation without revealing value
    if isinstance(value, str):
        h = hashlib.sha256(value.encode()).hexdigest()[:8]
    else:
        h = hashlib.sha256(value).hexdigest()[:8]
    return f"[REDACTED:sha256:{h}]"


def load_secret_from_env(name: str, environ: dict | None = None) -> bytes | None:
    """Load secret from process env only; never from .env file. Redacts on error."""
    if environ is None:
        environ = dict(os.environ)
    val = environ.get(name)
    if not val:
        return None
    # Validate without logging
    try:
        validate_secret(name, val)
    except ValueError as exc:
        # Do not include value in error
        raise ValueError(f"Invalid {name}: {exc}") from None
    return val.encode() if isinstance(val, str) else val


def scan_for_forbidden_text(text: str) -> list[str]:
    """Return matched forbidden patterns without returning secret content."""
    hits: list[str] = []
    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, text):
            hits.append(pat)
    return hits


def assert_no_secrets_in_repo(repo_path: Path | None = None) -> list[str]:
    """Scan repo for forbidden patterns; used by CI."""
    if repo_path is None:
        repo_path = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for p in repo_path.rglob("*.py"):
        # Skip pycache, .venv
        if ".venv" in str(p) or "__pycache__" in str(p):
            continue
        try:
            content = p.read_text(errors="ignore")
        except Exception:
            continue
        hits = scan_for_forbidden_text(content)
        if hits:
            offenders.append(f"{p}: {hits}")
    # Check .env not committed (should be gitignored)
    env = repo_path / ".env"
    if env.exists():
        offenders.append(".env file must not exist in repo")
    return offenders
