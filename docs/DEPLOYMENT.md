# Deployment — Ai-earn (2026-11-16, M15)

**Status:** Local development only, `production_ready: false`. No deployment is approved. This document is the runbook for local POSIX/Windows and the future architecture. M15 adds read-only health checks, backup/restore drill, and CI hardening while keeping `production_ready:false`.

## Local development (current, LATEST 22, 21 backups)

- **Python:** 3.11+ (3.13 verified on Windows by user at `f26c6b9`), stdlib only. No `pyproject.toml`, no `requirements.txt`.
- **Setup (POSIX):**
  ```sh
  python3 -m venv .venv
  .venv/bin/python -m ai_earn status          # {production_ready:false, component:offline_foundation}
  mkdir -m 700 ~/.ai-earn-private
  export AI_EARN_AUTH_DB=~/.ai-earn-private/auth.sqlite3
  export AI_EARN_STATE_DB=~/.ai-earn-private/state.sqlite3
  .venv/bin/python -m ai_earn auth register  # hidden password, one-time code (save privately)
  .venv/bin/python -m ai_earn tracker init   # type YES, creates 21 backups before-v2..v22
  .venv/bin/python -m ai_earn tracker open   # 48-choice menu
  .venv/bin/python -m ai_earn health --json  # read-only ok:false→true after init
  .venv/bin/python -m ai_earn doctor         # read-only, immutable=1
  .venv/bin/python -m ai_earn dashboard snapshot  # beside state DB, 1 MB cap, counts only
  python -m http.server 8000 --bind 127.0.0.1  # then open /dashboard/app.html (17 views) or /dashboard/connected.html
  ```
- **Windows:** Same but via `create_private_directory` helper, fixed NTFS drive with persistent ACLs, no reparse points, PowerShell `Join-Path`, `.\\.venv\\Scripts\\python.exe`. See `docs/WINDOWS_TESTING.md` and `WINDOWS_PHASE3_TESTING.md`.
- **Health:** `python -m ai_earn health` (human) / `health --json` / `health --json --with-backups` — checks `config` (AI_EARN_ENV valid), `auth`/`state` private + `integrity_check` + `application_id` + `user_version` in `SUPPORTED_STATE_VERSIONS`, `version` (LATEST 22), optional `backups` (21 expected). Exit 0 when `ok:true`, 1 when not ok, 2 when config invalid. Never reads secrets. Always `production_ready:false`.
- **Tests:** `.venv/bin/python -m compileall -q ai_earn tests tools && .venv/bin/python -m unittest discover -v` (823/17, 41 focused including `test_health` + `test_frontend_spa`).
- **Static dashboard:** `dashboard/app.html` (vanilla SPA, hash router, dark/light, responsive, no network fetch, 1 MB limit, a11y). No HTTP API listener; `python -m http.server --bind 127.0.0.1` is the only intended local server.

## Secrets

- **Never in repo:** No `AI_EARN_*_DB` path, password, `webhook_secret`, or recovery code is committed. `.gitignore` excludes `*.sqlite3`, `*.sqlite3-*`, `dashboard-snapshot-*.json`, `.env`.
- **Env only:** `AI_EARN_ENV` (`development`|`test`|`production`), `AI_EARN_LOG_LEVEL`, `AI_EARN_AUTH_DB`, `AI_EARN_STATE_DB` are absolute paths validated by `load_settings`/`load_database_paths` without leaking values (errors name the variable only). `ProviderConfig.webhook_secret` (32..128) is validated by `validate_api_key` and `redact_config` before logging.
- **Rotation:** `python -m ai_earn auth register` issues a one-time recovery code; `replace_recovery_code` requires live session + `confirmed=True`. Provider secrets are per-env stub only; live is blocked (`Live AI provider is not enabled`, `Live provider not enabled`).
- **CI secret scan:** `grep -R`/`git diff --check` for `password`, `secret`, `BEGIN PRIVATE KEY` is run locally before push; GitHub secret scanning remains enabled. No `pip audit` needed (stdlib only).

## Health checks (M15, implemented)

- **Local CLI:** `python -m ai_earn health --json` → `{ok, version, production_ready:false, checks:{config,auth,state,version,backups}}`. Mirrors `doctor` classification (`missing`/`permission_error`/`not_a_database`/`unsupported_schema`/…/`ok`) plus version/backups. Read-only, `immutable=1` opens.
- **Future HTTP:** `GET /healthz` → same payload, `200` when `ok:true`, `503` when not ok — not yet implemented (no listener). Current SPA and tracker call `health` locally only.
- **CI gate:** `.github/workflows/tests.yml` runs on `ubuntu+windows × 3.11/3.13`: `setup-python` → `compileall` → `unittest discover -v` → `doctor --json` (expected non-ok on clean runner, validates CLI), `health --json` is exercised in `tests/test_health.py` (9 tests). No network.

## Backups & rollout

- **Migrations:** `migrate_to_vN` does `backup_to` while holding write lock, then DDL + verification, transactional. New files start at v1; existing files never auto-upgraded. Keep `before-v2..v22` chain forever (gitignored).
- **Manual backup:** `python -c "from pathlib import Path; from ai_earn.state_storage import StateStorage; s=StateStorage(Path(env['AI_EARN_STATE_DB']), create=False); s.backup_to(Path(str(s.path)+'.bak'), confirmed=True)"` — never `cp --force` over live.
- **Restore:** `backup_to(new_private_path, confirmed=True)` to **new** path, then swap `AI_EARN_STATE_DB` after `health --with-backups` + `doctor` are `ok`. Old snapshot `operation_key` reuse can create a second lease — see `docs/BACKUP_AND_RECOVERY.md` and `test_phase3_recovery_rehearsal`.

## Production architecture (not approved, M15 checklist)

- **App tier:** If a web is added later, bind `127.0.0.1` first, then private-beta with `Secure; HttpOnly; SameSite` cookies, CSRF, rate limits, allowlisted `Event` logging. No `Dockerfile`/`fly.toml` yet — do not invent before M15 exit review.
- **Storage:** Local SQLite is **not** for multi-user production; evaluate Postgres RLS or SQLite-per-user vault. `doctor`/`health` will gate any prod candidate.
- **CI:** `compileall` + `unittest` (823/17) + `git diff --check` + secret-pattern grep. `windows_phase3_gate` (41 focused) must pass on clean Windows checkout before any prod tag.
- **Rollback:** Keep `before-v*.sqlite3` chain; restore is copy-to-new-path, never `REPLACE`. Drill on disposable copy weekly.

Preserve warning: “Never select a directory under untrusted ancestors” (`windows_security._check_local_ancestors`). `production_ready` remains `false` until independent security, recovery, and legal reviews sign off.
