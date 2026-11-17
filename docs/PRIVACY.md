# Privacy — Ai-earn (2026-11-16, Phase 16)

**Status:** Experimental, local-offline only. No cloud storage, no third-party sharing. This document describes data minimization, purpose, retention, export and deletion. It is not legal advice.

## What data is stored

All data is **local SQLite** in a private owner-only directory you create (`~/.ai-earn-private` POSIX 0700 / Windows protected ACL). No data leaves the device except when you explicitly export a snapshot or data export file.

**Auth DB (`AI_EARN_AUTH_DB`, schema 1):**
- `accounts`: id (random), username (3–32), password_salt (16 B), password_hash (PBKDF2-HMAC-SHA256 600k), recovery_hash (SHA-256 of one-time code), created_at. No plaintext password or recovery code is stored.
- `sessions`: token_hash (SHA-256), account_id, expires_at (30 min), revoked_at. Raw token exists only in process memory.
- `auth_attempts` / `auth_audit`: action, subject, timestamps, event names only — no secrets.

**State DB (`AI_EARN_STATE_DB`, v1→v28, APPLICATION_ID 0x41494533):**
- Financial drafts, opportunities (title, budget, URL), services, products, customers (name/contact/source), communications (body), ledger, followups, invoices, provider sandbox orders/events (HMAC-verified sandbox only), AI approvals, project tasks, CRM notes/orders, tax/billing. All rows are **owner-scoped** (`owner_id` = account id). See `docs/DATABASE.md`.
- No card storage is ever created — `PRAGMA table_info(payment_provider_orders)` has no `card` column (verified by `production_readiness`).
- Snapshots (`dashboard-snapshot-*.json`) contain **aggregate counts/amounts only** — no username, title, or token. They are written beside the state DB with `O_EXCL|O_NOFOLLOW` (POSIX) / `create_private_file` (Windows), 1 MB cap.

## Purpose & legal basis

Purpose is a disposable local tracker for unverified personal business drafts. No real customer data should be entered until production readiness is `PASS` and you have a lawful basis and consent. Do not enter third-party personal data without consent.

## Retention & backups

- Live DBs are retained until you delete them. `before-v*.sqlite3` backups are created **before each migration** via `backup_to` while holding the write lock; keep them forever until you explicitly delete them. They are the migration audit trail and recovery source.
- `backup.py` recommends 30-day retention for user-initiated backups (`RETENTION_DAYS = 30`) — delete old `*.bak` after verifying new backup with `doctor` + `health --with-backups`.
- All files are gitignored (`*.sqlite3*`, `*.db*`, `dashboard-snapshot-*.json`, `.env`).

## Sharing

No sharing by default. Export files are created **only when you run** `dashboard snapshot` or `data_export`. They stay in your private directory; you decide whether to move them. Never share a snapshot or export containing real customer data over insecure channels.

## Your rights — export & deletion

- **Export:** Owner-scoped, bounded, redacted JSON. Requires live session, verifies health, never exports password/recovery hashes, truncates at 50 MB, writes to same private directory with `0o600`. See `ai_earn/data_export.py` `export_user_data(token, auth_db, state_db, destination)`. CLI: `python -m ai_earn data export` (interactive, hidden password, confirmation). See `docs/USER_GUIDE.md`.
- **Deletion:** Two-phase safeguards. `prepare_deletion_backup` creates verified backup; `confirm_deletion` requires password re-entry + typed phrase `DELETE <8-char owner prefix>` + rate limit. Deletes **only your owner rows** across 40+ tables (children first), revokes sessions, removes account row, preserves backup. Never deletes another owner's data. See `ai_earn/account_deletion.py`. CLI: `python -m ai_earn data delete` (interactive).
- **Verification:** After export/deletion, run `python -m ai_earn health --json --with-backups` and `python -m ai_earn doctor --json` to confirm store states.

## Logging & monitoring

Logs are structured JSON (see `ai_earn/structured_logging.py`) with allowlisted events only, redacted secrets/paths, and no DB content. Health monitoring (`ai_earn/monitoring.py`) polls `health` locally; no network telemetry.

## Children & sensitive data

Do not store children's data or highly sensitive categories (health, biometrics) — the local store has no field-level encryption beyond OS file ACLs.

## Contact & complaints

For privacy questions, see `SECURITY.md` and `THREAT_MODEL.md`. For data requests, use export/deletion above. Until `production_ready:true`, this is not a hosted service with external DPAs.

---
**Do not expose the local SQLite database over the network.** Any HTTP server must bind `127.0.0.1` only; never `0.0.0.0` and never serve `*.sqlite3` or `*.json` exports. See `docs/DEPLOYMENT.md`.
