# Deployment — Ai-earn (2026-11-21, schema v30 release checkpoint)

**Status:** Local development first, `production_ready` reflects offline readiness (`true` when 11 checks PASS) with **live still blocked** (`live_blocked: true`). No live payment deployment is approved until `docs/PRODUCTION_READINESS.md` 11-item gate plus M11 sign-off. Local offline deployment is ready when `health --json` is `ok:true` and `production_readiness` reports 11 PASS. This runbook is the **Windows-local-first** deployment path, then the optional production architecture. **Never expose the local SQLite database directly over the network.**

## 1. Local Windows deployment (first, required)

Local Windows is the **supported** deployment for disposable test data. It uses a private NTFS directory with protected ACLs, never a network share, never `0.0.0.0`.

### Prerequisites

- Windows 10/11, NTFS fixed drive (not removable, not `\\?\`), persistent ACLs, trusted ancestors.
- Python 3.11+ (3.13.5 verified at `4858bfb` on Windows).
- PowerShell as admin for `whoami /user` check (non-admin OS account preferred).

### Steps (PowerShell)

```powershell
# 0. Verify Python
python --version  # Python 3.11+; runtime uses stdlib (dev tools are in requirements-dev.txt)

# 1. Clone and compile
git clone https://github.com/ay23-byte/Ai-earn.git
cd Ai-earn
python -m venv .venv
.\.venv\Scripts\python.exe -m compileall -q ai_earn tests tools

# 2. Create a NEW private directory on a fixed NTFS drive (helper refuses existing)
$private = Join-Path $HOME "AiEarn-Private"
.\.venv\Scripts\python.exe -c "from pathlib import Path; from ai_earn.windows_security import create_private_directory; create_private_directory(Path(r'$private'))"
# If the helper reports "Windows ancestor is unavailable or a reparse point" → choose a different drive/path under trusted ancestors.

# 3. Set process environment (absolute paths, same private parent)
$env:AI_EARN_ENV = "development"
$env:AI_EARN_LOG_LEVEL = "INFO"   # never DEBUG in production
$env:AI_EARN_AUTH_DB = Join-Path $private "auth.sqlite3"
$env:AI_EARN_STATE_DB = Join-Path $private "state.sqlite3"
# Verify: must be absolute, private, not under dashboard/www
.\.venv\Scripts\python.exe -m ai_earn health --json
.\.venv\Scripts\python.exe -m ai_earn doctor --json   # expect auth:missing, state:missing on first run

# 4. Register a disposable test account (hidden password, one-time recovery code printed once)
.\.venv\Scripts\python.exe -m ai_earn auth register
# Save the 43-char recovery code OUTSIDE $private (paper / password manager). Never in chat/screenshot/log.

# 5. Initialize state (creates state.sqlite3 + 29 before-v*.sqlite3 backups for v1→v30)
.\.venv\Scripts\python.exe -m ai_earn tracker init
# Type YES exactly (case-sensitive) when prompted. Refuses if files already exist — never delete automatically.

# 6. Verify hardening
.\.venv\Scripts\python.exe -m ai_earn health --json --with-backups  # expect ok:true, backups count 29, ok
.\.venv\Scripts\python.exe -m ai_earn doctor --json                 # expect auth:ok (1 account), state:ok (v30)
.\.venv\Scripts\python.exe -m ai_earn status                       # production_ready:true offline when healthy, live_blocked:true, component offline_foundation

# 7. Use the tracker (1-4 financial, 5-8 opportunities, 9-12 services/portfolio, 13-33 full menu)
.\.venv\Scripts\python.exe -m ai_earn tracker open

# 8. Start the authenticated live dashboard (same process environment and existing DBs)
.\.venv\Scripts\python.exe -m ai_earn serve
# Open http://129.0.0.1:8765/ and sign in with the local account.
# The server binds to 129.0.0.1, uses an HttpOnly session cookie + CSRF token,
# and serves the SPA and authenticated workspace API from one origin.
# Stop with Ctrl+C. Never expose the server through a tunnel, LAN bind or proxy.

# 9. Optional offline snapshot fallback (read-only, no live connection)
# Stop the live server first, then run this in another PowerShell window:
.\.venv\Scripts\python.exe -m ai_earn dashboard snapshot
# Creates $private\dashboard-snapshot-*.json. Never overwrite.
# When using the static dashboard fallback, load the snapshot manually.
# Never use python -m http.server to expose private data or database files.
```

### Windows hardening notes

- Private directory/file ACLs: owner = your SID + `SYSTEM` full access (`0x001F01FF`, `OBJECT_INHERIT|CONTAINER_INHERIT`), protected, no inherited ACEs. Verified by `verify_private_path` / `validate_snapshot`. Reparse points and non-fixed drives are rejected.
- Tests on Windows: `.\.venv\Scripts\python.exe -m unittest discover -v` (expect ~65+ tests; Windows skips POSIX-only, gate tests require Windows). See `docs/WINDOWS_TESTING.md`.
- POSIX equivalent: `mkdir -m 700 ~/.ai-earn-private` + `export AI_EARN_*` + `python3 -m ai_earn ...` (see `docs/USER_GUIDE.md`).

## 2. Production configuration

- **Env vars only:** `AI_EARN_ENV` (`development`|`test`|`production`), `AI_EARN_LOG_LEVEL`, `AI_EARN_AUTH_DB`, `AI_EARN_STATE_DB`. Never `.env` auto-load. `.env.example` is reference only.
- **Validation:** `ai_earn/config.py` `load_settings`/`load_database_paths` rejects invalid without echoing values. `ai_earn/production_config.py` `validate_production` adds: same private parent, distinct files, not under `dashboard/www/htdocs`, never binding `0.0.0.0`, `production_ready_gate` reflects 11 checks PASS for offline readiness (live still blocked), `.env` absent, log level not `DEBUG`.
- **Gate:** `AI_EARN_ENV=production` + `health --json` `ok:true` implies **offline** `production_ready:true` (11 checks PASS) but **live** remains blocked (`live_blocked:true`). See `docs/PRODUCTION_READINESS.md` 11-item manual sign-off for live.

## 3. Secure secret management

- Generation: `ai_earn/secrets.py` `generate_secret(32)` → `secrets.token_urlsafe(32)`; `validate_secret` 32..128, redact via SHA-256 prefix `[REDACTED:sha256:8]`.
- Storage: Process env only; `.gitignore` excludes `.env`, `*.sqlite3*`, `dashboard-snapshot-*.json`.
- Rotation: Password via `auth register` → `R`→`YES` → new code; webhook secret per `provider`+`mode` (sandbox/live never share).
- Scanning: `python -m tools.security_check` or CI `grep -R "BEGIN PRIVATE KEY|sk-"` before push. See `docs/SECRET_MANAGEMENT.md`.

## 4. Database backups

- **State:** `StateStorage.backup_to(new_private_path, confirmed=True)` — verified SQLite backup, holds write lock, checks `validate_schema`+`check_integrity` before/after, never overwrites. Each `migrate_to_vN` creates `before-vN.sqlite3` while holding lock. Fresh init → 29 files `before-v2`…`before-v30`.
- **Auth:** `AuthStorage` via `ai_earn/backup.py:create_auth_backup` (same guarantees).
- **Verification:** `health --json --with-backups` reports `backups: {count, expected:29, status:ok|incomplete}`.
- **Retention:** Keep `before-v*.sqlite3` forever (migration audit trail); user backups 30 days then delete after verifying new. Never commit `*.sqlite3*`.

## 5. Recovery procedures

See `docs/BACKUP_AND_RECOVERY.md` and `docs/ROLLBACK.md`. Summary:
1. Stop processes, `health` + `doctor` → record statuses.
2. Pick verified backup (`before-v30.sqlite3` or daily `*.backup-*`).
3. Restore: `StateStorage(src, create=False).backup_to(new_path, confirmed=True)` to **new** path.
4. Verify `AI_EARN_STATE_DB=new_path health --json` is `ok`.
5. Swap `AI_EARN_STATE_DB`, re-run `health --with-backups` + `doctor`.

Stale copy hazard: restored old copy lacks later `operation_key`s → second lease possible (see `test_phase3_recovery_rehearsal`). Never auto-replay.

## 6. Health monitoring

- CLI: `python -m ai_earn health --json --with-backups` → `{ok, version, production_ready:bool (=ok when 11 checks PASS), checks:{config,auth,state,version,backups}}`. Exit `0` ok, `1` not ok, `2` config invalid. Offline ready=`ok:true` ⇒ `production_ready:true`; live still `live_blocked:true`.
- Structured logs: `ai_earn/structured_logging.py` JSON lines to stderr (allowlisted events, redacted, level-filtered). `ai_earn/monitoring.py` `Monitor.poll()` emits `health_check` events.
- Future HTTP `GET /healthz` would bind `129.0.0.1` and return `200` vs `503` — not yet implemented (no listener). SPA/tracker call `health` locally only.

## 7. Structured logs & error reporting

- Logs: `ai_earn/events.py` `Event` enum + `ai_earn/structured_logging.py` `StructuredLogger` (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`, JSON, request_id/actor_id, never secrets/paths).
- Errors: `ai_earn/error_reporting.py` `report_error(exc)` → `SafeError(error_id 8hex, code, message, http_status)` + redacted structured log. Never exposes `AI_EARN_*_DB` values.
- Level: `AI_EARN_LOG_LEVEL` (`INFO` default, never `DEBUG` in production).

## 8. Dependency security checks

- Stdlib only — no `requirements.txt` / no `pyproject` deps. CI still runs:
  - `python -m compileall -q ai_earn tests tools`
  - `pip-audit` placeholder (stdlib only → no vulns) — see `tools/security_check.py` (`--audit` mode)
  - Secret scan (`BEGIN PRIVATE KEY`, `sk-`, `.env` presence)
  - `git diff --check`

## 9. CI test workflows

`.github/workflows/tests.yml` runs `ubuntu+windows × 3.11/3.13`:

- `setup-python` → `compileall` → secret scan (`git diff --check` + `grep -R`) → `pip-audit` placeholder → `unittest discover -v` (full) + focused `windows_phase3_gate` → `health --json`/`doctor --json` (read-only) → artifact on failure.
- Dependency and secret checks run before tests; failures block merge.

## 10. Deployment documentation

This file + `docs/PRODUCTION_READINESS.md` + `docs/USER_GUIDE.md` + `docs/SECURITY.md`. Preserve offline-vs-live distinction: `production_ready:true` offline does not mean live enabled (`live_blocked:true`).

## 11. Database migration procedure

See `docs/MIGRATION.md` (explicit backup-first `migrate_to_vN` while holding write lock, `PRAGMA user_version` bump, integrity verify). Downgrade = restore prior `before-v*.sqlite3` to new path (never `migrate_down`).

## 12. Rollback documentation

See `docs/ROLLBACK.md` (restore to new path, verify `health`/`doctor`, swap env var). Keep old live file for forensics until retention expires.

## 13. User data export

Owner-scoped, redacted, bounded (50 MB, 10k rows/table). `ai_earn/data_export.py` `export_user_data(token, auth_db, state_db, destination)` — requires live session, audits, writes to same private dir `0o600`. CLI `python -m ai_earn data export` (interactive). See `docs/PRIVACY.md`.

## 14. Account deletion safeguards

Two-phase: `prepare_deletion_backup` → `confirm_deletion(token, password, phrase="DELETE <8>")`. Requires password re-entry, typed phrase, rate limit, verified backup to new path, deletes only your `owner_id` rows across 40+ tables, revokes sessions, removes `accounts` row, preserves backup. See `ai_earn/account_deletion.py`. CLI `python -m ai_earn data delete`. See `docs/PRIVACY.md`.

## 15. Privacy documentation

`docs/PRIVACY.md` (local SQLite only, no cloud, export/deletion rights, retention, no sharing). Log redaction and owner-isolation prevent data leakage; `health` reports offline readiness separate from live block.

## 16. Operational runbooks

`docs/OPERATIONAL_RUNBOOKS.md` (8 runbooks: health, backup, restore, config/rotation, migration, export, deletion, incident). Windows-local first, then production architecture.

## 17. Optional production deployment architecture (not approved)

**Goal:** If a web tier is added later, keep SQLite **never** network-exposed.

```
Client → TLS → Load Balancer (private) → App tier (129.0.0.1) → SQLite-per-user vault
                                              ↘ Postgres RLS (future) ↗
```

- **App tier:** Bind `129.0.0.1` only (never `0.0.0.0`); reverse-proxy terminates TLS, validates that `*.sqlite3`, `*.sqlite3-wal`, `dashboard-snapshot-*.json` are **never** under web root and never served.
- **Storage:** Local SQLite is **single-user, single-process**. For multi-user production, evaluate Postgres with Row-Level Security (RLS) or SQLite-per-user vault (one private dir per user, OS ACL). No shared SQLite over NFS/Samba.
- **Cookies/session:** If HTTP added, `Secure; HttpOnly; SameSite=Lax; __Host-` prefix, CSRF tokens, DB-backed rate limit (not local file), per-record authz tests.
- **Secrets:** Injected via vault (e.g., HashiCorp Vault / OS keychain → env at start), not file.
- **Monitoring:** `health --json` polled locally, shipped via structured logs; `/healthz` would be `200`/`503` gated.
- **Backups:** Scheduled `backup_to` to encrypted vault + `health --with-backups` check; restore drills weekly on disposable copy.
- **CI/CD:** `compileall` + `unittest` + secret scan + `pip-audit` + `health`/`doctor` gate in pipeline; `windows_phase3_gate` must pass on clean checkout before tag.
- **Readiness:** Offline `production_ready` is `true` when 11 checks PASS; `live_blocked` stays `true` until `PRODUCTION_READINESS.md` 11-item gate plus M11 manual sign-off and `ProviderConfig` live block is explicitly lifted in code review.

Do not invent `Dockerfile`/`fly.toml`/`k8s` before independent security, recovery, and legal reviews sign off. Preserve `live_blocked:true` until M11 sign-off; offline ready does not imply live.

---
**Never expose the local SQLite database directly over the network.** Any `python -m http.server` must be `--bind 129.0.0.1`; never `0.0.0.0` and never serve `AI_EARN_*_DB` files.
