# Backup and Recovery — Ai-earn (2026-11-21, schema v30)

**Status:** Experimental, local SQLite only. No auto-repair. **Do not delete or overwrite files blindly.** M15 adds `health --with-backups` and a disposable restore drill.

## What is backed up

- **Auth DB:** `AI_EARN_AUTH_DB` (`auth.sqlite3`) – single account expected; `recovery_hash` rotation on `replace_recovery_code`/`recover`.
- **State DB:** `AI_EARN_STATE_DB` (`state.sqlite3`, v1→v30) plus 29 `before-v*.sqlite3` backups (`before-v2.sqlite3` … `before-v30.sqlite3`) created by `tracker init`/`migrate_to_v*`.
- **Snapshots:** `dashboard-snapshot-*.json` beside state DB (counts only, no titles/tokens; 1 MB cap; never overwrite).

## How backups are made (current code)

- `StateStorage.backup_to(destination, confirmed=True)`:
  1. Validates source (`validate_schema` + `check_integrity` → `quick_check`, `foreign_key_check`, creation-event, terminal-transition, multi-step replay, queue consistency, etc.), holds write lock.
  2. `create_exclusive` on destination (fails if exists, checks parent privacy, excludes sidecars).
  3. `source.backup(destination)` (SQLite online backup), verifies destination.
  4. Leaves partial file on failure for inspection; never replaces.
- **Migrations:** `migrate_to_vN` does `backup_to` **while holding write lock**, then DDL, then verification, all in one `transaction`. New files start at v1.
- **Health backup check:** `python -m ai_earn health --json --with-backups` reports `backups: {count, expected:29, status:ok|incomplete}` when state is `ok`; otherwise `not_checked`.

## How restores work (manual, human-approved)

- Restore = `backup_to` from trusted backup **to a new private path**, then swap via env var after `health` + `doctor` are `ok`. Never overwrite live file.
- Stale restore hazard: old copy lacks later `operation_key`s, can create second lease on same key (rehearsal `test_phase3_recovery_rehearsal` + `test_frontend_spa`/`test_health` document the hazard). `compare_snapshot`/`compare_records` return aggregate counts only, not proof of freshness.
- **P0-4 mitigation:** After any `backup_to` restore to a new private path, run `doctor --check-stale` (or `health --json` with `AI_EARN_AUTH_BACKUP` set) — it compares `auth_audit` timestamps read-only. If `stale`, first login with old password succeeds (hazard), but `health`/`doctor` warn `stale` with `requires_rotation:true` and `delta_seconds`. Mitigate by revoking sessions on the restored copy and rotating recovery code:
  ```python
  from pathlib import Path
  from ai_earn.backup import detect_stale_auth_backup, handle_stale_restore, revoke_all_sessions
  detect_stale_auth_backup(Path("/private/auth.sqlite3"), Path("/backup/auth.backup.sqlite3"))
  # if stale:
  handle_stale_restore(Path("/private/auth.sqlite3"), Path("/backup/auth.backup.sqlite3"), Path("/private/restored.sqlite3"), confirmed=True)
  # or directly:
  revoke_all_sessions(Path("/private/restored.sqlite3"), confirmed=True)
  # then login and: AuthService(...).replace_recovery_code(token, confirmed=True)
  ```
  Never use stale copy without this step; old recovery codes/sessions revive silently.

## Doctor & health diagnostics

- `python -m ai_earn doctor` – read-only, `permission_error` for unsafe parent, `missing` for absent file, `empty_file`, `not_a_database`, `unsupported_schema`/`incompatible_schema` (bad `state_migrations` history), `ok`.
- `python -m ai_earn doctor --plan-init` – human recovery steps (inspect, restore, or new private dir + `tracker init` with YES).
- `python -m ai_earn doctor --json` / `python -m ai_earn health --json` / `python -m ai_earn health --json --with-backups` — machine-readable; `health` adds `ok` + `backups` + `stale` + `production_ready:bool` (=ok when 11 checks PASS offline, live still blocked).
- `python -m ai_earn doctor --check-stale` / `python -m ai_earn doctor --check-stale --json` / `AI_EARN_AUTH_BACKUP=/path/to/backup.sqlite3 python -m ai_earn health --json` — read-only stale check comparing `auth_audit MAX(occurred_at)` between primary `AI_EARN_AUTH_DB` and backup `AI_EARN_AUTH_BACKUP` (also `AI_EARN_STATE_BACKUP` for state). Returns `stale` when backup older (requires `replace_recovery_code` + session revocation), `ok` when current, `not_checked` when no backup path.
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
  ls -1 "$tmp"/before-v*.sqlite3 2>/dev/null | wc -l  # expect 29 after fresh init (LATEST 30)
  # Exercise backup_to to new path (never overwrites)
  python -c "from pathlib import Path, os; from ai_earn.state_storage import StateStorage; s=StateStorage(Path(os.environ['AI_EARN_STATE_DB']), create=False); s.backup_to(Path(os.environ['AI_EARN_STATE_DB']+'.bak2'), confirmed=True)"
  AI_EARN_AUTH_DB="$tmp/auth.sqlite3" AI_EARN_STATE_DB="$tmp/state.sqlite3.bak2" python -m ai_earn health --json
  rm -rf "$tmp"
  ```
  Windows PowerShell equivalent uses `Join-Path $env:TEMP` + `python -c` with `pathlib`.
- **CI drill:** `tests/test_health.py` (9 tests) does the same on `TemporaryDirectory` with `StateStorage(..., create=True)` → `migrate_to_v30` → `health` + `health --with-backups` + `backup_to(new_path)` verification.
- Real-data recovery remains unverified; no existing user DB has been migrated (per `PHASE_STATUS.md`).

## Retention & encryption (P0-3, 2026-11-17)

- **Chain forever:** Keep `before-v*.sqlite3` forever until explicit user deletion; they are the migration audit trail (29 files for LATEST 30). Git ignores `*.sqlite3*` so they never commit. `prune_backups()` and `list_prunable_backups()` never return this chain — defense in depth even if a `*.backup-*` glob matched.
- **Ephemeral prunable set:** Only `*.backup-*`, `*.bak`, `*.bak.*`, and `dashboard-snapshot-*.json` are prunable. `state.sqlite3` / `auth.sqlite3` and encrypted `*.enc`/`*.age` are never pruned automatically.
- **Never auto-delete:** No automatic deletion on init/migration/health. User runs dry-run first:
  ```python
  from pathlib import Path
  from ai_earn.backup import prune_backups, backup_retention_status
  backup_retention_status(Path("/private/dir"))  # chain 29 expected, prunable counts
  prune_backups(Path("/private/dir"), keep=7, dry_run=True)   # review would_delete
  prune_backups(Path("/private/dir"), keep=7, dry_run=False, confirmed=True)  # actually delete oldest
  ```
  `keep=29+7` mental model: 29 chain (protected forever) + 7 newest ephemeral kept; `prune_backups(keep=7)` keeps 7 ephemeral newest, oldest deleted. `prune_backups` never overwrites, requires `confirmed=True` when `dry_run=False`, and refuses to delete chain/main DBs.
- **Plaintext until vault:** `dashboard-snapshot-*.json` + `*.sqlite3` and `*.backup-*` remain plaintext 0600 (POSIX) / explicit ACL (Windows) until age/libsodium or OS keychain approved. Do not store backups on shared/synced drives.
- **STUB authenticated encryption (stdlib only, NOT production-grade):** `ai_earn.backup.encrypt_backup(path, key=bytes, confirmed=True)` and `decrypt_backup(enc_path, dest, key=...)` provide an HMAC-SHA256 authenticated, PBKDF2-derived keystream wrapper so disposable drills can prove `backup_to -> encrypt (.enc/.age) -> decrypt -> StateStorage verify / integrity_check` without new deps. Never logs keys, never overwrites, 50 MB cap. Real deployment must replace with `age` or `libsodium sealed box` + OS keychain (`AI_EARN_BACKUP_KEY_{key_id}` or `AI_EARN_BACKUP_KEY` env, 32..256 chars via `secrets.validate_secret`). Alternate `suffix=".age"` accepted for docs parity.
  ```python
  from pathlib import Path
  from ai_earn.backup import encrypt_backup, decrypt_backup, verify_backup
  import secrets
  key = secrets.token_bytes(32)  # or key_id="prod" -> env AI_EARN_BACKUP_KEY_prod
  enc = encrypt_backup(Path("/private/dir/state.sqlite3"), key=key, confirmed=True)  # -> state.sqlite3.enc
  dec = Path("/private/dir/restored.sqlite3")
  decrypt_backup(enc, dec, key=key, confirmed=True)
  assert verify_backup(dec)["status"] == "ok"
  from ai_earn.state_storage import StateStorage
  StateStorage(dec, create=False)  # validates schema + integrity
  ```
  Tampered ciphertext or wrong key -> `StateStorageError: authentication failed`; truncated file -> size/hash mismatch. For `auth.sqlite3` decrypt, verify with `AuthStorage` instead of `StateStorage`.
- **Snapshots:** `dashboard-snapshot-*.json` beside state DB (counts only, no titles/tokens; 1 MB cap; never overwrite) are also prunable after verifying new snapshot `ok`.

## Stale backup revival mitigation (P0-4, 2026-11-17)

- **Detection:** Read-only `MAX(occurred_at)` from `auth_audit` (`_auth_audit_max`) and `state_audit` (`_state_audit_max`). `ai_earn.backup.detect_stale_auth_backup(primary, backup)` and `detect_stale_state_backup` compare primary vs backup. If `backup MAX < primary MAX`, status `stale` with `delta_seconds`; else `ok` or `not_checked`. No secrets read, never writes.
- **Health:** `python -m ai_earn health --json` now includes `checks.stale` (`stale`/`ok`/`not_checked`/`invalid_configuration`). Set `AI_EARN_AUTH_BACKUP=/abs/path/to/backup.sqlite3` (and `AI_EARN_STATE_BACKUP` for state) to enable. `health --with-backups` also includes `stale`. `ok` stays true (auth/state ok) but `stale` warns with `requires_rotation:true`.
- **Doctor:** `python -m ai_earn doctor --json` includes `stale`. `python -m ai_earn doctor --check-stale` (or `--check-stale /path/to/backup --json`) prints human + JSON stale report and exits 1 when stale (so CI can gate). Also supports `AI_EARN_AUTH_BACKUP` env.
- **Mitigation flow (manual, confirmed):**
  1. Restore backup **to new private path** via `StateStorage.backup_to` or `create_auth_backup` (never overwrite).
  2. `python -m ai_earn doctor --check-stale /path/to/backup --json` → if `stale`, `health` will also warn.
  3. `revoke_all_sessions(restored_path, confirmed=True)` → `UPDATE sessions SET revoked_at=now WHERE revoked_at IS NULL` + audit `stale_restore_sessions_revoked` (requires private parent).
  4. Login on restored copy, then `replace_recovery_code(token, confirmed=True)` (or `recover`) to rotate `recovery_hash`.
  5. Re-run `health`/`doctor --check-stale` → `ok` (backup now considered current or not_checked if no longer comparing).
- **Acceptance:** Disposable test: `register → login token1 → backup → recover (100s later, revokes token1) → backup is stale (delta 100s) → restore backup → token1 revives and old password works → health stale warns → handle_stale_restore(..., confirmed=True) revokes 2 sessions → token1 denied → replace_recovery_code required.` See `ai_earn/backup.py:handle_stale_restore` and health/doctor stale checks.

## Runbook excerpt (local)

1. Stop all processes holding DBs.
2. `python -m ai_earn health --json --with-backups` + `python -m ai_earn doctor --json` → record statuses.
3. If `permission_error` → fix dir/file ACL (POSIX `chmod 700/600`, Windows `verify_private_path`).
4. If `missing` auth → restore from latest auth backup to same path, re-run health/doctor.
5. If `missing` state + auth `ok` → `python -m ai_earn tracker init` with YES (only if no `before-v*.sqlite3` exists), or restore state backup to new path and `health`/`doctor` → `ok` → swap env.
6. If `unsupported_schema` → do not auto-migrate; inspect `state_migrations` history, restore known-good backup to new path.
7. Never `rm -rf` private dir; always new dir for new account if backup lost. Offline `production_ready` is `true` when healthy and 11 checks PASS; live remains `live_blocked:true` until M11.
