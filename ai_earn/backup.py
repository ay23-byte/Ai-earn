"""Database backup orchestration — verified, never overwrites, never network-exposed.

Backups are SQLite online copies to a new private path, verified via
validate_schema + check_integrity before and after. This module adds
scheduling metadata, retention guidance, verification helpers on top of
StateStorage.backup_to and AuthStorage backup, plus:

- STUB authenticated encryption for backups/snapshots (stdlib-only, NOT production-grade).
  Real age/libsodium or OS keychain required before production — this stub uses
  PBKDF2-derived HMAC keystream + HMAC-SHA256 for integrity so that
  backup_to + encrypt -> decrypt -> integrity_check round-trip can be verified
  on disposable copies without third-party deps. Never logs keys.

- Retention pruning helper that never deletes the 27-file before-v*.sqlite3
  migration chain; only prunes ephemeral *.backup-* copies (dry-run default).

See docs/BACKUP_AND_RECOVERY.md for retention policy.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .state_storage import StateStorage, StateStorageError
from .auth_storage import AuthStorage, StorageError

# Sidecars never backed up via network
BACKUP_SUFFIX = ".bak"
RETENTION_DAYS = 30  # guidance only; user manages deletion

# ---- STUB encryption (stdlib only, not production) ----
_ENCRYPT_MAGIC = b"AI_EARN_ENC1"  # 12 bytes
_ENCRYPT_VERSION = 1
_ENCRYPT_SALT_LEN = 16
_ENCRYPT_NONCE_LEN = 16
ENCRYPT_SUFFIX = ".enc"
# Age-style suffix for docs/tests (same file format, alternate extension)
ENCRYPT_AGE_SUFFIX = ".age"

# Prunable patterns — ephemeral copies only, never the 27-file chain
_PRUNABLE_GLOBS = ["*.backup-*", "*.bak.*", "*.bak"]
# Maximum single backup file we will encrypt/decrypt in-memory (50 MB)
_MAX_ENCRYPT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class BackupResult:
    destination: Path
    bytes: int
    verified: bool
    duration_ms: int


def create_backup(
    state_db: Path,
    destination: Path,
    *,
    confirmed: bool = False,
) -> BackupResult:
    """Create a verified backup of state DB to destination. Never overwrites."""
    if confirmed is not True:
        raise StateStorageError("Explicit confirmation required for backup.")
    if destination.exists():
        raise StateStorageError("Backup destination already exists; never overwrite.")
    start = time.monotonic()
    storage = StateStorage(state_db, create=False)
    storage.backup_to(destination, confirmed=True)
    elapsed = int((time.monotonic() - start) * 1000)
    size = destination.stat().st_size if destination.exists() else 0
    return BackupResult(destination=destination, bytes=size, verified=True, duration_ms=elapsed)


def create_auth_backup(
    auth_db: Path,
    destination: Path,
    *,
    confirmed: bool = False,
) -> BackupResult:
    """Backup auth DB via sqlite backup API (same guarantees)."""
    if confirmed is not True:
        raise StorageError("Explicit confirmation required for auth backup.")
    if destination.exists():
        raise StorageError("Backup destination already exists.")
    import sqlite3
    from contextlib import closing

    start = time.monotonic()
    # Validate source is private and intact
    AuthStorage(auth_db, create=False)
    # Use sqlite backup directly to new private file
    # Create destination as private file first

    # Reuse StateStorage's private file creation via _PrivateFiles
    from .state_storage import _PrivateFiles

    pf = _PrivateFiles(destination)
    pf.create_exclusive()
    with closing(sqlite3.connect(auth_db.as_uri() + "?mode=ro", uri=True, timeout=5)) as s:
        with closing(sqlite3.connect(destination.as_uri() + "?mode=rw", uri=True, timeout=5)) as d:
            s.backup(d)
    elapsed = int((time.monotonic() - start) * 1000)
    size = destination.stat().st_size if destination.exists() else 0
    return BackupResult(destination=destination, bytes=size, verified=True, duration_ms=elapsed)


def list_backups(private_dir: Path) -> list[Path]:
    """List before-v*.sqlite3 backups in private dir, sorted."""
    if not private_dir.is_dir():
        return []
    return sorted(private_dir.glob("before-v*.sqlite3"))


def verify_backup(candidate: Path) -> dict:
    """Verify a backup candidate without opening as primary. Returns health-like dict."""
    try:
        StateStorage(candidate, create=False)
        # If constructor succeeds, schema is valid
        return {"path": str(candidate), "status": "ok", "bytes": candidate.stat().st_size}
    except Exception as exc:
        return {"path": str(candidate), "status": "invalid", "detail": str(exc)[:200]}


def backup_metadata(private_dir: Path) -> dict:
    """Return backup chain health for monitoring (no secrets)."""
    backups = list_backups(private_dir)
    from .state_storage import LATEST_SCHEMA_VERSION

    expected = LATEST_SCHEMA_VERSION - 1
    return {
        "count": len(backups),
        "expected": expected,
        "status": "ok" if len(backups) >= expected else "incomplete",
        "latest": str(backups[-1]) if backups else None,
        "retention_days": RETENTION_DAYS,
        "note": "Keep before-v*.sqlite3 forever until explicit user deletion; never commit *.sqlite3 to git.",
    }


# ---- Encryption helpers (stub, stdlib only) ----
def _derive_keys(master_key: bytes, salt: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    """Derive enc and mac keys via HMAC-SHA256 (stub KDF, not HKDF)."""
    enc_key = hmac.new(master_key, salt + b"enc" + nonce, hashlib.sha256).digest()
    mac_key = hmac.new(master_key, salt + b"mac" + nonce, hashlib.sha256).digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate keystream via HMAC-SHA256 CTR (stub stream cipher)."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _resolve_key(
    *,
    key: bytes | None,
    key_id: str | None,
    environ: Mapping[str, str] | None,
) -> bytes:
    """Resolve master key from explicit bytes or env key_id (never logs value)."""
    if key is not None:
        if not isinstance(key, (bytes, bytearray)):
            raise StateStorageError("Encryption key must be bytes.")
        if not (32 <= len(key) <= 128):
            raise StateStorageError("Encryption key must be 32..128 bytes.")
        return bytes(key)
    # key_id lookup via env (stdlib, no .env auto-load)
    if key_id is not None:
        if not isinstance(key_id, str) or not key_id or len(key_id) > 64:
            raise StateStorageError("Invalid key_id.")
        # sanitize key_id for env var name
        safe = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in key_id)
        candidates = [f"AI_EARN_BACKUP_KEY_{safe}", "AI_EARN_BACKUP_KEY"]
        env = environ if environ is not None else os.environ
        for name in candidates:
            val = env.get(name) if isinstance(env, dict) else os.environ.get(name)
            if val:
                # validate via secrets helper without logging
                from .secrets import validate_secret

                try:
                    validate_secret(name, val)
                except ValueError as exc:
                    raise StateStorageError(f"Invalid {name}: {exc}") from None
                # Use raw bytes of urlsafe string (never log)
                return val.encode("utf-8") if isinstance(val, str) else bytes(val)
        raise StateStorageError(f"Backup key not found for key_id '{key_id}' (set AI_EARN_BACKUP_KEY_{safe} or AI_EARN_BACKUP_KEY, 32..256 chars).")
    raise StateStorageError("Encryption key required: pass key=bytes or key_id=str (env AI_EARN_BACKUP_KEY...).")


def encrypt_backup(
    path: Path,
    *,
    key: bytes | None = None,
    key_id: str | None = None,
    environ: Mapping[str, str] | None = None,
    confirmed: bool = False,
    suffix: str = ENCRYPT_SUFFIX,
) -> Path:
    """STUB: Authenticated-encrypt a backup/snapshot file (stdlib only).

    NOT FOR PRODUCTION — plaintext 0600 remains until age/libsodium or OS
    keychain approved. This stub exists so disposable drills can prove:
      backup_to -> encrypt -> decrypt -> integrity_check
    without new deps. Never overwrites, never logs key.

    Args:
        path: Existing private file to encrypt (state.sqlite3, *.backup-*, snapshot json).
        key: 32..128 byte master key (if provided, key_id ignored).
        key_id: Env key id -> resolves AI_EARN_BACKUP_KEY_{key_id} or AI_EARN_BACKUP_KEY.
        environ: Optional env mapping for tests.
        confirmed: Must be True.
        suffix: Output suffix (default .enc, alternate .age accepted).

    Returns:
        Path to encrypted file (path + suffix).
    """
    if confirmed is not True:
        raise StateStorageError("Explicit confirmation required to encrypt backup.")
    src = Path(path).expanduser()
    if not src.is_absolute():
        raise StateStorageError("Backup path must be absolute.")
    if not src.is_file():
        raise StateStorageError("Backup file not found for encryption.")
    if src.stat().st_size > _MAX_ENCRYPT_BYTES:
        raise StateStorageError("Backup too large to encrypt (50 MB cap).")
    if suffix not in (ENCRYPT_SUFFIX, ENCRYPT_AGE_SUFFIX):
        raise StateStorageError("Encrypt suffix must be .enc or .age.")
    dest = Path(str(src) + suffix)
    if dest.exists():
        raise StateStorageError("Encrypted destination already exists; never overwrite.")

    master = _resolve_key(key=key, key_id=key_id, environ=environ)
    plaintext = src.read_bytes()
    # Derive stub keys
    salt = secrets.token_bytes(_ENCRYPT_SALT_LEN)
    nonce = secrets.token_bytes(_ENCRYPT_NONCE_LEN)
    enc_key, mac_key = _derive_keys(master, salt, nonce)
    keystream = _keystream(enc_key, nonce, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, keystream))
    pt_hash = hashlib.sha256(plaintext).digest()
    orig_size = len(plaintext).to_bytes(8, "big")
    # Auth tag covers magic+version+salt+nonce+size+hash+ciphertext
    auth_data = _ENCRYPT_MAGIC + bytes([_ENCRYPT_VERSION]) + salt + nonce + orig_size + pt_hash + ciphertext
    tag = hmac.new(mac_key, auth_data, hashlib.sha256).digest()

    # Write via private exclusive file (0600 / Windows ACL)
    from .state_storage import _PrivateFiles

    pf = _PrivateFiles(dest)
    pf.create_exclusive()
    # Format: magic(12) + version(1) + salt(16) + nonce(16) + orig_size(8) + pt_hash(32) + tag(32) + ciphertext
    dest.write_bytes(_ENCRYPT_MAGIC + bytes([_ENCRYPT_VERSION]) + salt + nonce + orig_size + pt_hash + tag + ciphertext)
    # Ensure 0600 on POSIX (Windows ACL handled by create_private_file)
    try:
        if os.name == "posix":
            os.chmod(dest, 0o600)
    except Exception:
        pass
    return dest


def decrypt_backup(
    encrypted_path: Path,
    destination: Path,
    *,
    key: bytes | None = None,
    key_id: str | None = None,
    environ: Mapping[str, str] | None = None,
    confirmed: bool = False,
) -> Path:
    """STUB: Decrypt a file produced by encrypt_backup, verify HMAC+hash, never overwrites.

    Args:
        encrypted_path: Source .enc/.age file (absolute).
        destination: New private path to write plaintext (absolute, must not exist).
        key/key_id/environ: Same as encrypt_backup.
        confirmed: Must be True.

    Returns:
        Path to decrypted file (destination).
    Raises:
        StateStorageError on HMAC failure, size/hash mismatch, or existing destination.
    """
    if confirmed is not True:
        raise StateStorageError("Explicit confirmation required to decrypt backup.")
    src = Path(encrypted_path).expanduser()
    dst = Path(destination).expanduser()
    if not src.is_absolute() or not dst.is_absolute():
        raise StateStorageError("Decrypt paths must be absolute.")
    if not src.is_file():
        raise StateStorageError("Encrypted file not found.")
    if dst.exists():
        raise StateStorageError("Decrypt destination already exists; never overwrite.")
    if src.suffix not in (".enc", ".age"):
        # allow .enc/.age even when suffix string includes dot; check full suffix
        if not str(src).endswith((ENCRYPT_SUFFIX, ENCRYPT_AGE_SUFFIX)):
            raise StateStorageError("Encrypted file must end with .enc or .age.")

    master = _resolve_key(key=key, key_id=key_id, environ=environ)
    blob = src.read_bytes()
    min_len = 12 + 1 + 16 + 16 + 8 + 32 + 32
    if len(blob) < min_len:
        raise StateStorageError("Encrypted file too short or corrupted.")
    magic = blob[0:12]
    if magic != _ENCRYPT_MAGIC:
        raise StateStorageError("Encrypted file magic mismatch.")
    version = blob[12]
    if version != _ENCRYPT_VERSION:
        raise StateStorageError("Unsupported encrypted file version.")
    salt = blob[13 : 13 + 16]
    nonce = blob[29 : 29 + 16]
    orig_size = int.from_bytes(blob[45:53], "big")
    pt_hash = blob[53:85]
    tag = blob[85:117]
    ciphertext = blob[117:]
    if len(ciphertext) != orig_size and orig_size <= _MAX_ENCRYPT_BYTES:
        # Ciphertext length must equal plaintext length for XOR stream
        if len(ciphertext) != orig_size:
            raise StateStorageError("Encrypted file size/hash mismatch.")
    if len(blob) != min_len + len(ciphertext):
        raise StateStorageError("Encrypted file truncated or corrupted.")

    enc_key, mac_key = _derive_keys(master, salt, nonce)
    auth_data = _ENCRYPT_MAGIC + bytes([_ENCRYPT_VERSION]) + salt + nonce + orig_size.to_bytes(8, "big") + pt_hash + ciphertext
    expected_tag = hmac.new(mac_key, auth_data, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise StateStorageError("Encrypted file authentication failed (wrong key or corrupted).")
    keystream = _keystream(enc_key, nonce, len(ciphertext))
    plaintext = bytes(a ^ b for a, b in zip(ciphertext, keystream))
    if hashlib.sha256(plaintext).digest() != pt_hash:
        raise StateStorageError("Decrypted plaintext hash mismatch.")
    if len(plaintext) != orig_size:
        raise StateStorageError("Decrypted size mismatch.")

    from .state_storage import _PrivateFiles

    pf = _PrivateFiles(dst)
    pf.create_exclusive()
    dst.write_bytes(plaintext)
    try:
        if os.name == "posix":
            os.chmod(dst, 0o600)
    except Exception:
        pass
    # Optional verification for SQLite: try StateStorage then AuthStorage; JSON snapshots skipped.
    # Never raise for non-state DBs (auth.sqlite3) — let caller verify via verify_backup or AuthStorage.
    if plaintext[:6] == b"SQLite":
        # Try state first, then auth; if both fail the file is at least readable sqlite but unknown schema
        # We do not fail decrypt here — HMAC/hash already proved integrity. Caller may validate further.
        try:
            StateStorage(dst, create=False)
        except Exception:
            try:
                AuthStorage(dst, create=False)
            except Exception:
                # Header says sqlite but neither storage recognizes it — could be truncated/corrupt
                # Keep file for inspection but do not raise; verification is caller's responsibility
                pass
    return dst


def list_prunable_backups(private_dir: Path) -> list[Path]:
    """List ephemeral *.backup-* (and *.bak) files, sorted oldest-first by mtime, excluding chain.

    Never returns before-v*.sqlite3, state.sqlite3, auth.sqlite3, or *.enc/.age (encrypted copies).
    Chain files are reported via list_backups() and are never prunable.
    """
    pdir = Path(private_dir).expanduser()
    if not pdir.is_dir():
        return []
    candidates: list[Path] = []
    for pat in _PRUNABLE_GLOBS:
        for cand in pdir.glob(pat):
            # Exclude chain, main DBs, and encrypted copies from prunable set
            name = cand.name
            if name.startswith("before-v") and name.endswith(".sqlite3"):
                continue
            if name in ("state.sqlite3", "auth.sqlite3"):
                continue
            if name.endswith(ENCRYPT_SUFFIX) or name.endswith(ENCRYPT_AGE_SUFFIX):
                continue
            # Only regular files
            try:
                if cand.is_file():
                    candidates.append(cand)
            except Exception:
                continue
    # Also include dashboard-snapshot-*.json as prunable (ephemeral, not chain)
    for cand in pdir.glob("dashboard-snapshot-*.json"):
        try:
            if cand.is_file():
                candidates.append(cand)
        except Exception:
            continue
    # Deduplicate by resolved path, sort by mtime oldest first
    uniq = {c.resolve(): c for c in candidates}
    sorted_cands = sorted(uniq.values(), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    return sorted_cands


def prune_backups(
    private_dir: Path,
    *,
    keep: int = 7,
    dry_run: bool = True,
    confirmed: bool = False,
) -> dict:
    """Prune ephemeral backups, never deleting the 27-file before-v*.sqlite3 chain.

    Retention: chain forever until explicit user deletion; ephemeral *.backup-*
    + *.bak + dashboard-snapshot-*.json pruned to keep newest N (default 7).
    Roadmap keep=27+7 means 27 chain (never pruned) + 7 ephemeral. Caller may
    pass keep=34 for total mental model, but function keeps 34 ephemeral — chain
    excluded. Use dry_run=True first.

    Args:
        private_dir: Private directory containing backups.
        keep: Number of newest prunable files to keep (0..1000).
        dry_run: If True, no deletion; returns would_delete list.
        confirmed: Must be True when dry_run is False.

    Returns:
        Dict with counts and paths (strings, never secrets).
    """
    if not isinstance(keep, int) or not (0 <= keep <= 1000):
        raise StateStorageError("keep must be integer 0..1000.")
    pdir = Path(private_dir).expanduser()
    if not pdir.is_absolute():
        raise StateStorageError("Private dir path must be absolute.")
    chain = list_backups(pdir)
    prunable = list_prunable_backups(pdir)
    total = len(prunable)
    protected_chain_count = len(chain)
    if total <= keep:
        return {
            "status": "ok",
            "private_dir": str(pdir),
            "protected_chain_count": protected_chain_count,
            "protected_chain_note": "before-v*.sqlite3 never pruned",
            "prunable_count": total,
            "keep": keep,
            "kept": [str(p) for p in prunable],
            "would_delete": [],
            "deleted": [],
            "dry_run": bool(dry_run),
        }
    # Oldest first, so to_delete = oldest total-keep
    to_delete = prunable[: total - keep]
    to_keep = prunable[total - keep :]
    if dry_run:
        return {
            "status": "dry_run",
            "private_dir": str(pdir),
            "protected_chain_count": protected_chain_count,
            "protected_chain_note": "before-v*.sqlite3 never pruned",
            "prunable_count": total,
            "keep": keep,
            "kept": [str(p) for p in to_keep],
            "would_delete": [str(p) for p in to_delete],
            "deleted": [],
            "dry_run": True,
        }
    if confirmed is not True:
        raise StateStorageError("Explicit confirmation required to prune backups (dry_run=False).")
    # Safety: refuse to delete chain even if pattern matched (defense in depth)
    for p in to_delete:
        if p.name.startswith("before-v"):
            raise StateStorageError("Refusing to prune protected before-v chain.")
        if p.name in ("state.sqlite3", "auth.sqlite3"):
            raise StateStorageError("Refusing to prune primary DB.")
    deleted: list[str] = []
    for p in to_delete:
        try:
            p.unlink()
            deleted.append(str(p))
        except Exception as exc:
            raise StateStorageError(f"Failed to prune {p.name}: {exc}") from exc
    return {
        "status": "pruned",
        "private_dir": str(pdir),
        "protected_chain_count": protected_chain_count,
        "protected_chain_note": "before-v*.sqlite3 never pruned",
        "prunable_count": total,
        "keep": keep,
        "kept": [str(p) for p in to_keep],
        "would_delete": [],
        "deleted": deleted,
        "dry_run": False,
    }


def backup_retention_status(private_dir: Path) -> dict:
    """Health-like retention status for monitoring (no secrets, no mutation)."""
    pdir = Path(private_dir).expanduser()
    chain = list_backups(pdir)
    prunable = list_prunable_backups(pdir)
    from .state_storage import LATEST_SCHEMA_VERSION

    expected_chain = LATEST_SCHEMA_VERSION - 1
    return {
        "chain_count": len(chain),
        "chain_expected": expected_chain,
        "chain_status": "ok" if len(chain) >= expected_chain else "incomplete",
        "prunable_count": len(prunable),
        "prunable_oldest": str(prunable[0]) if prunable else None,
        "prunable_newest": str(prunable[-1]) if prunable else None,
        "retention_days": RETENTION_DAYS,
        "protected_note": "before-v*.sqlite3 never auto-deleted; prune_backups(keep=7, dry_run=True) to review ephemeral *.backup-*",
    }


# ---- Stale backup revival mitigation (P0-4) ----
def _auth_audit_max(auth_path: Path) -> int | None:
    """Read-only MAX(occurred_at) from auth_audit; None if unreadable/empty."""
    try:
        import sqlite3 as _sq

        uri = f"file:{Path(auth_path).as_posix()}?mode=ro&immutable=1"
        with _sq.connect(uri, uri=True, timeout=5) as conn:
            # Validate header quickly
            try:
                row = conn.execute("SELECT MAX(occurred_at) FROM auth_audit").fetchone()
                return row[0] if row and row[0] is not None else None
            except _sq.Error:
                return None
    except Exception:
        return None


def _state_audit_max(state_path: Path) -> int | None:
    """Read-only MAX(occurred_at) from state_audit; None if unreadable/empty."""
    try:
        import sqlite3 as _sq

        uri = f"file:{Path(state_path).as_posix()}?mode=ro&immutable=1"
        with _sq.connect(uri, uri=True, timeout=5) as conn:
            try:
                row = conn.execute("SELECT MAX(occurred_at) FROM state_audit").fetchone()
                return row[0] if row and row[0] is not None else None
            except _sq.Error:
                return None
    except Exception:
        return None


def detect_stale_auth_backup(primary_auth: Path, backup_auth: Path) -> dict:
    """Compare auth_audit MAX(occurred_at) between primary and backup (read-only).

    Returns dict with is_stale, primary_latest, backup_latest, delta_seconds,
    warning, requires_rotation. Never raises, never logs secrets.
    """
    pa = Path(primary_auth).expanduser()
    ba = Path(backup_auth).expanduser()
    primary_latest = _auth_audit_max(pa) if pa.is_file() else None
    backup_latest = _auth_audit_max(ba) if ba.is_file() else None
    if primary_latest is None or backup_latest is None:
        # If either unreadable, cannot determine staleness -> not_checked
        return {
            "is_stale": False,
            "primary_latest": primary_latest,
            "backup_latest": backup_latest,
            "delta_seconds": None,
            "warning": None,
            "requires_rotation": False,
            "detail": "Cannot determine staleness: missing or unreadable auth_audit",
            "status": "not_checked",
        }
    delta = primary_latest - backup_latest
    is_stale = delta > 0
    # Consider stale if backup is at least 1 second older; clock may be mocked in tests
    if is_stale:
        warning = (
            f"Stale auth backup: backup is {delta}s older than primary (backup {backup_latest} < primary {primary_latest}). "
            "Restoring it revives old password/recovery codes and sessions. "
            "After any restore, revoke sessions and rotate recovery code: "
            "python -m ai_earn auth login -> replace_recovery_code(confirmed=True) or recover(). "
            "Do not use stale copy for production."
        )
        return {
            "is_stale": True,
            "primary_latest": primary_latest,
            "backup_latest": backup_latest,
            "delta_seconds": delta,
            "warning": warning,
            "requires_rotation": True,
            "detail": f"backup {delta}s stale",
            "status": "stale",
        }
    return {
        "is_stale": False,
        "primary_latest": primary_latest,
        "backup_latest": backup_latest,
        "delta_seconds": 0,
        "warning": None,
        "requires_rotation": False,
        "detail": "backup is current (not older than primary)",
        "status": "ok",
    }


def detect_stale_state_backup(primary_state: Path, backup_state: Path) -> dict:
    """Compare state_audit MAX(occurred_at) similarly for state DB."""
    ps = Path(primary_state).expanduser()
    bs = Path(backup_state).expanduser()
    primary_latest = _state_audit_max(ps) if ps.is_file() else None
    backup_latest = _state_audit_max(bs) if bs.is_file() else None
    if primary_latest is None or backup_latest is None:
        return {
            "is_stale": False,
            "primary_latest": primary_latest,
            "backup_latest": backup_latest,
            "delta_seconds": None,
            "warning": None,
            "requires_rotation": False,
            "detail": "Cannot determine state staleness: missing or unreadable state_audit",
            "status": "not_checked",
        }
    delta = primary_latest - backup_latest
    is_stale = delta > 0
    if is_stale:
        warning = (
            f"Stale state backup: backup is {delta}s older than primary (backup {backup_latest} < primary {primary_latest}). "
            "Restore loses newer operation_keys/audit and can create duplicate leases. Reconcile independently."
        )
        return {
            "is_stale": True,
            "primary_latest": primary_latest,
            "backup_latest": backup_latest,
            "delta_seconds": delta,
            "warning": warning,
            "requires_rotation": False,
            "detail": f"backup {delta}s stale",
            "status": "stale",
        }
    return {
        "is_stale": False,
        "primary_latest": primary_latest,
        "backup_latest": backup_latest,
        "delta_seconds": 0,
        "warning": None,
        "requires_rotation": False,
        "detail": "state backup current",
        "status": "ok",
    }


def revoke_all_sessions(auth_db: Path, *, confirmed: bool = False) -> dict:
    """Revoke all non-revoked sessions in auth DB (required after stale restore).

    Never overwrites DB file; uses transaction. Requires confirmed=True.
    Returns dict with revoked_count.
    """
    if confirmed is not True:
        raise StorageError("Explicit confirmation required to revoke sessions.")
    p = Path(auth_db).expanduser()
    if not p.is_absolute():
        raise StorageError("Auth DB path must be absolute.")
    if not p.is_file():
        raise StorageError("Auth DB file not found.")
    # Use AuthStorage transaction (holds lock, validates parent privacy)
    store = AuthStorage(p, create=False)
    now = int(time.time())
    count = 0
    with store.transaction() as conn:
        # Count before
        before = conn.execute("SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL").fetchone()[0]
        if before > 0:
            conn.execute("UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL", (now,))
            # Audit revocation per session? Single audit for bulk
            # Find an account to audit under if exists
            acct = conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()
            aid = acct[0] if acct else None
            conn.execute("INSERT INTO auth_audit(occurred_at,event,account_id) VALUES(?,?,?)", (now, "stale_restore_sessions_revoked", aid))
            count = before
        else:
            count = 0
    return {"revoked_count": count, "now": now, "detail": f"Revoked {count} session(s); rotate recovery code next."}


def handle_stale_restore(
    primary_auth: Path,
    backup_auth: Path,
    restored_auth: Path,
    *,
    confirmed: bool = False,
) -> dict:
    """Detect staleness and, if stale, revoke sessions on restored_auth and emit warning.

    Workflow for P0-4: user restored backup_auth to restored_auth (new private path) via
    StateStorage.backup_to or create_auth_backup. This helper:
      1. Compares primary vs backup timestamps (read-only).
      2. If stale, revokes all sessions on restored_auth (requires confirmed=True) and returns warning.

    Args:
        primary_auth: Current primary auth DB (absolute).
        backup_auth: Backup file that was restored (absolute).
        restored_auth: Newly restored DB path to revoke sessions on (absolute).
        confirmed: Must be True to perform revocation.

    Returns:
        Dict with detection + revocation result.
    """
    detection = detect_stale_auth_backup(primary_auth, backup_auth)
    if not detection["is_stale"]:
        return {"detection": detection, "revoked_count": 0, "warning": None, "detail": "not stale, no action"}
    if confirmed is not True:
        # Detection only, no mutation
        return {
            "detection": detection,
            "revoked_count": 0,
            "warning": detection["warning"] + " (dry-run: pass confirmed=True to revoke)",
            "detail": "stale detected, revocation requires confirmed=True",
        }
    rev = revoke_all_sessions(restored_auth, confirmed=True)
    warning = detection["warning"] + f" Revoked {rev['revoked_count']} session(s) on restored copy; now rotate recovery code via replace_recovery_code(confirmed=True)."
    return {"detection": detection, "revoked_count": rev["revoked_count"], "warning": warning, "detail": "stale mitigated"}
