# Deployment — Ai-earn (2026-11-16)

**Status:** Local development only, `production_ready: false`. No deployment is approved. This document is the runbook for local Windows/ POSIX and the future architecture.

## Local development (current)

- **Python:** 3.11+ (3.13 verified on Windows by user at `f26c6b9`), stdlib only.
- **Setup (POSIX):**
  ```sh
  python3 -m venv .venv
  .venv/bin/python -m ai_earn status
  mkdir -m 700 ~/.ai-earn-private
  export AI_EARN_AUTH_DB=~/.ai-earn-private/auth.sqlite3
  export AI_EARN_STATE_DB=~/.ai-earn-private/state.sqlite3
  .venv/bin/python -m ai_earn auth register  # hidden password, one-time code
  .venv/bin/python -m ai_earn tracker init   # YES, 13 backups
  .venv/bin/python -m ai_earn tracker open
  .venv/bin/python -m ai_earn doctor         # read-only
  .venv/bin/python -m ai_earn dashboard snapshot
  python -m http.server 8000 --bind 127.0.0.1  # then open /dashboard/connected.html
  ```
- **Windows:** Same but via `create_private_directory` helper, fixed local drive with persistent ACLs, no reparse points, PowerShell `Join-Path`, `.\.venv\Scripts\python.exe`. See `docs/WINDOWS_TESTING.md` and `WINDOWS_PHASE3_TESTING.md`.
- **Tests:** `.venv/bin/python -m unittest discover -v` (636/17 baseline), `python -m compileall -q ai_earn tests tools`.

## Future local Windows deployment path (planned, M15)

- Keep DBs on fixed NTFS drive with persistent ACLs, separate private dir per env (`dev` vs `test`), no network share.
- Future web (when added) must bind `127.0.0.1` only for local MVP, then document private-beta network path with session cookies, CSRF, rate limits.
- Never expose SQLite file directly over network; always via service layer with `WHERE owner_id=?`.

## Production architecture (optional, not approved)

- App tier: Python service (gunicorn/uvicorn after framework decision ADR-006), `Secure; HttpOnly; SameSite` cookies, health `/healthz`.
- Storage: local SQLite is **not** for multi-user production; evaluate Postgres with row-level security or SQLite-per-user with backup vault. Migrations remain backup-first.
- Secrets: vault (e.g., env via systemd/Cloud secret manager), never in repo, CI secret scan.
- CI: `python -m compileall`, `unittest`, `pip audit` (when deps added), `git diff --check`.
- Rollback: keep `before-v*.sqlite3` chain, `backup_to` to new path, never `REPLACE`.

No `Dockerfile`, `fly.toml`, or cloud config exists; do not invent one before M15.

## Health checks (future)

- `GET /healthz` → `{ok: true, version, sha, checks: {db: ok, auth: ok}}`
- Not implemented; `python -m ai_earn status` is the current health signal.

Preserve warning: “Never select a directory under untrusted ancestors” (`windows_security._check_local_ancestors`).
