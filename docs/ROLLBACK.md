# Rollback — Ai-earn (Phase 16)

Rollback is **restore of a prior verified backup to a new private path**. There is no `migrate_down` and no in-place mutation of a live DB.

## Triggers

- Migration fails (`PRAGMA integrity_check` != `ok`, `foreign_key_check` fails, or `doctor` reports `incompatible_schema` after attempt).
- App regression after upgrade (health `ok:false`, tests fail, snapshot cannot be exported).
- Need to revert to last known-good data before a bulk import.

## Safeguards

- Never overwrite a live `*.sqlite3`; destination must be **new** and exclusive (never `REPLACE`/`OVERWRITE`).
- Backups are SQLite online copies with integrity verification before and after.
- Old copy lacks later `operation_key`s — replaying it can create a second lease on the same key (documented in `BACKUP_AND_RECOVERY.md`). Treat restored copies as point-in-time, not proof of freshness.
- Always verify with `health --json --with-backups` + `doctor --json` before swapping env.

## Procedure

```sh
# 1. List available backups (private dir only, never network)
ls -lht "$(dirname $AI_EARN_STATE_DB)"/before-v*.sqlite3
python -m ai_earn health --json --with-backups   # note expected count = LATEST-1 = 27
python -m ai_earn doctor --json

# 2. Pick a backup (example before-v28.sqlite3 = state at v27)
SRC="$(dirname $AI_EARN_STATE_DB)/before-v28.sqlite3"
DST="$(dirname $AI_EARN_STATE_DB)/state.sqlite3.rollback-$(date +%Y%m%d-%H%M%S)"
python -c "
from pathlib import Path
from ai_earn.state_storage import StateStorage
src = Path('$SRC')
dst = Path('$DST')
StateStorage(src, create=False).backup_to(dst, confirmed=True)
print('restored', dst)
"

# 3. Verify restored copy without touching live
AI_EARN_AUTH_DB="$AI_EARN_AUTH_DB" AI_EARN_STATE_DB="$DST" python -m ai_earn health --json
AI_EARN_AUTH_DB="$AI_EARN_AUTH_DB" AI_EARN_STATE_DB="$DST" python -m ai_earn doctor --json
AI_EARN_AUTH_DB="$AI_EARN_AUTH_DB" AI_EARN_STATE_DB="$DST" python -m ai_earn dashboard snapshot --dry-run 2>&1 | head

# 4. Swap only after ok
export AI_EARN_STATE_DB="$DST"
python -m ai_earn health --json --with-backups   # should be ok
# Keep old live file for forensics; do not rm until retention expires
```

On Windows PowerShell replace `$(dirname ...)` with `Split-Path $env:AI_EARN_STATE_DB` and `date` with `Get-Date -Format yyyyMMdd-HHmmss`.

## Auth DB rollback

Same pattern with `ai_earn/backup.py:create_auth_backup` / `AuthStorage`. Auth has single version (1) so rollback is only to last file-system backup you made manually (`StateStorage` pattern does not auto-create `before-v*` for auth). Keep at least one `auth.sqlite3.*.bak` before any password/recovery rotation.

## Verification after rollback

- `health --json` `ok:true`, `state schema_version` = backup's version, `backups count >= expected`.
- `doctor` `state: ok`.
- `python -m unittest tests.test_state_migration -v` on disposable copy passes.
- Dashboard snapshot exports (aggregate counts) succeed.

If rollback still reports `incompatible_schema`, restore an earlier `before-v*.sqlite3` and repeat.

## Retention

Keep `before-v*.sqlite3` chain forever until explicit user deletion. They are gitignored and are the only downgrade path. See `docs/BACKUP_AND_RECOVERY.md`.
