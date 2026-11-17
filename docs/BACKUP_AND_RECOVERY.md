# Backup and Recovery — Ai-earn (2026-11-17, M15+M16, LATEST 28)

**Status:** Experimental, local SQLite only. No auto-repair. **Do not delete or overwrite files blindly.** M15 adds `health --with-backups` and a disposable restore drill.

## What is backed up

- **Auth DB:** `AI_EARN_AUTH_DB` (`auth.sqlite3`) – single account expected; `recovery_hash` rotation on `replace_recovery_code`/`recover`.
- **State DB:** `AI_EARN_STATE_DB` (`state.sqlite3`, v1→v28) plus 27 `before-v*.sqlite3` backups (`before-v2.sqlite3` … `before-v28.sqlite3`) created by `tracker init`/`migrate_to_v*`.
- **Snapshots:** `dashboard-snapshot-*.json` beside state DB (counts only, no titles/tokens; 1 MB cap; never overwrite).

## How backups are made (current code)

- `StateStorage.backup_to(destination, confirmed=True)`:
  1. Validates source (`validate_schema` + `check_integrity` → `quick_check`, `foreign_key_check`, creation-event, terminal-transition, multi-step replay, queue consistency, etc.), holds write lock.
  2. `create_exclusive` on destination (fails if exists, checks parent privacy, excludes sidecars).
  3. `source.backup(destination)` (SQLite online backup), verifies destination.
  4. Leaves partial file on failure for inspection; never replaces.
- **Migrations:** `migrate_to_vN` does `backup_to` **while holding write lock**, then DDL, then verification, all in one `transaction`. New files start at v1.
- **Health backup check:** `python -m ai_earn health --json --with-backups` reports `backups: {count, expected:21, status:ok|incomplete}` when state is `ok`; otherwise `not_checked`.

## How restores work (manual, human-approved)

- Restore = `backup_to` from trusted backup **to a new private path**, then swap via env var after `health` + `doctor` are `ok`. Never overwrite live file.
- Stale restore hazard: old copy lacks later `operation_key`s, can create second lease on same key (rehearsal `test_phase3_recovery_rehearsal` + `test_frontend_spa`/`test_health` document the hazard). `compare_snapshot`/`compare_records` return aggregate counts only, not proof of freshness.

## Doctor & health diagnostics

- `python -m ai_earn doctor` – read-only, `permission_error` for unsafe parent, `missing` for absent file, `empty_file`, `not_a_database`, `unsupported_schema`/`incompatible_schema` (bad `state_migrations` history), `ok`.
- `python -m ai_earn doctor --plan-init` – human recovery steps (inspect, restore, or new private dir + `tracker init` with YES).
- `python -m ai_earn doctor --json` / `python -m ai_earn health --json` / `python -m ai_earn health --json --with-backups` — machine-readable; `health` adds `ok` + `backups` + `production_ready:false`.
- JSON never contains passwords, tokens, or recovery codes.

## Recovery drills (M15, disposable)

- Temp DBs + `os._exit` before/after commit (already in `test_state_crash_recovery`, `test_task_queue`, `test_task_workflows`, `test_durable_sandbox_payments`, `test_health`), plus `compare_snapshot` divergent tests.
- **Manual drill on disposable copy (M15 runbook):**
  ```sh
  # POSIX
  tmp=$(mktemp -d) && chmod 700 "$tmp"
  cp "$AI_EARN_STATE_DB" "$tmp/state.sqlite3"
  cp "$AI_EARN_AUTH_DB" "$tmp/auth.sqlite3"
  AI_EARN_AUTH_DB="$tmp/auth.sqlite3" AI_EARN_STATE_DB="$tmp/state.sqlite3" python -m ai_earn health --json --with-backups
  AI_EARN_AUTH_DB="$tmp/auth.sqlite3" AI_EARN_STATE_DB="$tmp/state.sqlite3" python -m ai_earn doctor --json
  # Verify backup chain
  ls -1 "$tmp"/before-v*.sqlite3 2>/dev/null | wc -l  # expect 21 after fresh init
  # Exercise backup_to to new path (never overwrites)
  python -c "from pathlib import Path, os; from ai_earn.state_storage import StateStorage; s=StateStorage(Path(os.environ['AI_EARN_STATE_DB']), create=False); s.backup_to(Path(os.environ['AI_EARN_STATE_DB']+'.bak2'), confirmed=True)"
  AI_EARN_AUTH_DB="$tmp/auth.sqlite3" AI_EARN_STATE_DB="$tmp/state.sqlite3.bak2" python -m ai_earn health --json
  rm -rf "$tmp"
  ```
  Windows PowerShell equivalent uses `Join-Path $env:TEMP` + `python -c` with `pathlib`.
- **CI drill:** `tests/test_health.py` (9 tests) does the same on `TemporaryDirectory` with `StateStorage(..., create=True)` → `migrate_to_v28` → `health` + `health --with-backups` + `backup_to(new_path)` verification.
- Real-data recovery remains unverified; no existing user DB has been migrated (per `PHASE_STATUS.md`).

## Retention

- Keep `before-v*.sqlite3` forever until explicit user deletion; they are the migration audit trail. Git ignores `*.sqlite3*` so they never commit. Snapshots are also gitignored; rotate by deleting old `dashboard-snapshot-*.json` after verifying new one is `ok`.

## Runbook excerpt (local)

1. Stop all processes holding DBs.
2. `python -m ai_earn health --json --with-backups` + `python -m ai_earn doctor --json` → record statuses.
3. If `permission_error` → fix dir/file ACL (POSIX `chmod 700/600`, Windows `verify_private_path`).
4. If `missing` auth → restore from latest auth backup to same path, re-run health/doctor.
5. If `missing` state + auth `ok` → `python -m ai_earn tracker init` with YES (only if no `before-v*.sqlite3` exists), or restore state backup to new path and `health`/`doctor` → `ok` → swap env.
6. If `unsupported_schema` → do not auto-migrate; inspect `state_migrations` history, restore known-good backup to new path.
7. Never `rm -rf` private dir; always new dir for new account if backup lost. Keep `health` `production_ready:false` until sign-off.
