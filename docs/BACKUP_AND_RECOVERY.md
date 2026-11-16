# Backup and Recovery — Ai-earn (2026-11-16)

**Status:** Experimental, local SQLite only. No auto-repair. **Do not delete or overwrite files blindly.**

## What is backed up

- **Auth DB:** `AI_EARN_AUTH_DB` (`auth.sqlite3`) – single account expected; `recovery_hash` rotation on `replace_recovery_code`/`recover`.
- **State DB:** `AI_EARN_STATE_DB` (`state.sqlite3`, v1→v15) plus 14 `before-v*.sqlite3` backups (`before-v2.sqlite3` … `before-v15.sqlite3`) created by `tracker init`/`migrate_to_v*`.
- **Snapshots:** `dashboard-snapshot-*.json` beside state DB (counts only, no titles/tokens).

## How backups are made (current code)

- `StateStorage.backup_to(destination, confirmed=True)`:
  1. Validates source (`validate_schema` + `check_integrity`), holds write lock.
  2. `create_exclusive` on destination (fails if exists, checks parent privacy, excludes sidecars).
  3. `source.backup(destination)` (SQLite online backup), verifies destination.
  4. Leaves partial file on failure for inspection; never replaces.
- **Migrations:** `migrate_to_vN` does `backup_to` **while holding write lock**, then DDL, then verification, all in one `transaction`. New files start at v1.
- **Manual:** `cp` or `backup_to` to **new** path; never `cp --force` over existing.

## How restores work (manual, human-approved)

- Restore = `backup_to` from trusted backup **to a new private path**, then swap via env var after verification. Never overwrite live file.
- Stale restore hazard: old copy lacks later `operation_key`s, can create second lease on same key (rehearsal `test_phase3_recovery_rehearsal`). `compare_snapshot`/`compare_records` return aggregate counts only, not proof of freshness.

## Doctor diagnostics

- `python -m ai_earn doctor` – read-only, `permission_error` for unsafe parent, `missing` for absent file, `empty_file`, `not_a_database`, `unsupported_schema`/`incompatible_schema` (bad `state_migrations` history), `ok`.
- `python -m ai_earn doctor --plan-init` – human recovery steps (inspect, restore, or new private dir + `tracker init` with YES).
- JSON: `python -m ai_earn doctor --json`.

## Recovery drills (M2, disposable)

- Temp DBs + `os._exit` before/after commit (already in `test_state_crash_recovery`, `test_task_queue`, `test_task_workflows`, `test_durable_sandbox_payments`), plus `compare_snapshot` divergent tests.
- Manual drill on disposable copy:
  ```sh
  tmp=$(mktemp -d) && cp state.sqlite3 $tmp/state.sqlite3
  python -m ai_earn doctor  # before
  python -c "from ai_earn.state_storage import StateStorage; s=StateStorage(Path('state.sqlite3')); s.backup_to(Path('$tmp/backup.sqlite3'), confirmed=True)"
  python -m ai_earn doctor --json | jq .state.status
  ```
- Real-data recovery remains unverified; no existing user DB has been migrated (per `PHASE_STATUS.md`).

## Retention

- Keep `before-v*.sqlite3` forever until explicit user deletion; they are the migration audit trail. Git ignores `*.sqlite3*` so they never commit.

## Runbook excerpt (local)

1. Stop all processes holding DBs.
2. `python -m ai_earn doctor --json` → record statuses.
3. If `permission_error` → fix dir/file ACL (POSIX `chmod 700/600`, Windows `verify_private_path`).
4. If `missing` auth → restore from latest auth backup to same path, re-run doctor.
5. If `missing` state + auth `ok` → `python -m ai_earn tracker init` with YES (only if no `before-v*.sqlite3` exists), or restore state backup to new path and `compare_snapshot`.
6. Never `rm -rf` private dir; always new dir for new account if backup lost.
