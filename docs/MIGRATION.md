# Database Migration — Ai-earn (Phase 16, v1→v28)

**Invariant:** Migrations are **explicit, backup-first, never auto-run**, and hold the write lock from validation through commit. Downgrades do not exist — rollback is restore of a prior backup to a **new path**.

## Schema history

| Version | Tables / Indexes | Code |
|---|---|---|
| 1 | `state_meta`, `state_migrations`, empty `user_version=1`, `application_id` set | `StateStorage.__init__` |
| 2 | `state_audit`, `state_operations` | `migrate_to_v2` |
| 3–28 | See `state_storage.py` `_V3_SCHEMA`…`_V28_SCHEMA` + index constants (`_V6_INDEX` etc.) | `_apply_vN` |

Latest `LATEST_SCHEMA_VERSION = 28`, `SUPPORTED_STATE_VERSIONS = 1..28`.

## When to migrate

- After a code update that bumps `LATEST_SCHEMA_VERSION`.
- Only on a **private** state DB (`verify_private_path` / `0700` + `0600`), with no other process holding it.
- Never while `health --json` reports `permission_error` or `incompatible_schema`.

## Procedure (applies to any vN→vN+1)

```sh
# 0. Guard: health + doctor must be ok or missing with clear plan
python -m ai_earn health --json --with-backups
python -m ai_earn doctor --json

# 1. Backup current DB to a new private path (never overwrite)
#    Destination must not exist and be in same private parent.
python -c "
from pathlib import Path
import os
from ai_earn.state_storage import StateStorage
src = Path(os.environ['AI_EARN_STATE_DB'])
dst = src.parent / f\"before-v{src.stat().st_mtime:.0f}.sqlite3\"
StateStorage(src, create=False).backup_to(dst, confirmed=True)
print(dst)
"

# 2. Run the next migration only (example v27→v28)
python -c "
from pathlib import Path
import os
from ai_earn.state_storage import StateStorage
s = StateStorage(Path(os.environ['AI_EARN_STATE_DB']), create=False)
s.migrate_to_v28(Path(os.environ['AI_EARN_STATE_DB']).parent / 'before-v28.sqlite3', confirmed=True)
"

# 3. Verify
python -m ai_earn health --json --with-backups
python -m ai_earn doctor --json
sqlite3 \"$AI_EARN_STATE_DB\" \"PRAGMA user_version; PRAGMA application_id; PRAGMA integrity_check;\"
ls -l \"$(dirname $AI_EARN_STATE_DB)\"/before-v*.sqlite3   # expect 27 files after fresh v1→v28
```

**What `migrate_to_vN` does internally:**
1. Opens `BEGIN IMMEDIATE` (write lock), validates current `user_version == N-1` via `_validate_schema`.
2. Calls `backup_to` to `before-vN.sqlite3` (separate read-only connection, verifies integrity, creates exclusive file).
3. Executes DDL (`CREATE TABLE`/`ALTER TABLE`/`CREATE INDEX`), inserts `state_migrations(N, now)`, bumps `PRAGMA user_version = N`.
4. Re-validates schema + `_check_integrity` (quick_check, foreign_key_check, operation/audit balance, etc.) before commit.
5. On any failure, rolls back — no partial version bump.

## Batch init (fresh install)

`python -m ai_earn tracker init` does migrations v1→LATEST sequentially, each with its own backup, after you type `YES`. It refuses if the state file already exists. This creates `before-v2.sqlite3`…`before-v28.sqlite3` (27 files).

## Downgrade / rollback

No `migrate_down`. To rollback v28→v27, **restore** `before-v28.sqlite3` to a **new private path** (never overwrite live):

```sh
python -c "
from pathlib import Path
import os
from ai_earn.state_storage import StateStorage
src = Path(os.environ['AI_EARN_STATE_DB']).parent / 'before-v28.sqlite3'
dst = Path(os.environ['AI_EARN_STATE_DB']).parent / 'state.sqlite3.restored-v27'
StateStorage(src, create=False).backup_to(dst, confirmed=True)
"
AI_EARN_STATE_DB="$AI_EARN_STATE_DB.restored-v27" python -m ai_earn health --json --with-backups
AI_EARN_STATE_DB="$AI_EARN_STATE_DB.restored-v27" python -m ai_earn doctor --json
# Only after both are ok, swap env var
export AI_EARN_STATE_DB="$AI_EARN_STATE_DB.restored-v27"
```

See `docs/ROLLBACK.md` and `docs/BACKUP_AND_RECOVERY.md` (restore is copy-to-new-path).

## CI rehearsal

`tests/test_state_migration.py` and `tools/windows_phase3_gate.py` exercise every `migrate_to_vN` on disposable `TemporaryDirectory` with `StateStorage(create=True)` → `migrate_to_v28` → `health --with-backups` verification. Never on live DB.
