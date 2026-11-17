# Operational Runbooks — Ai-earn (Phase 16)

**Scope:** Local Windows-first deployment, owner-only SQLite. No network DB exposure. All commands require a real terminal; secrets are hidden input.

---

## Runbook 1 — Health check (daily)

```sh
python -m ai_earn health --json --with-backups
python -m ai_earn doctor --json
python -m ai_earn status   # production_ready must be false
```

**Structured logs:** `AI_EARN_LOG_LEVEL=INFO` writes JSON lines to stderr (allowlisted events, redacted). For aggregation, tail stderr: `python -m ai_earn health 2> health.log`.

**Alert on:**
- `config: invalid_configuration` → fix `AI_EARN_*` vars (see Runbook 4).
- `auth/state: permission_error` → fix dir/file ACL (0700/0600 or Windows ACL).
- `backups: incomplete` → expect 27 after fresh init; investigate missing `before-v*.sqlite3`.
- `ok:false` → follow `doctor --plan-init`.

Exit codes: `0` ok, `1` not ok, `2` config invalid.

---

## Runbook 2 — Backup (daily or before any mutation)

```sh
# POSIX
BACKUP_DIR="$(dirname $AI_EARN_STATE_DB)"
DST="$BACKUP_DIR/state.sqlite3.backup-$(date +%Y%m%d-%H%M%S)"
python -c "from pathlib import Path, os; from ai_earn.state_storage import StateStorage; s=StateStorage(Path(os.environ['AI_EARN_STATE_DB']), create=False); s.backup_to(Path('$DST'), confirmed=True)"
ls -lh "$DST"
python -m ai_earn health --json --with-backups  # verify count

# Windows PowerShell
$dst = Join-Path (Split-Path $env:AI_EARN_STATE_DB) "state.sqlite3.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
python -c "from pathlib import Path; import os; from ai_earn.state_storage import StateStorage; StateStorage(Path(os.environ['AI_EARN_STATE_DB']), create=False).backup_to(Path(r'$dst'), confirmed=True)"
```

Never `cp` or `overwrite`; destination must be **new**. Verify with `doctor` before relying on backup. Retention: 30 days (see `REBACKUP_DAYS`), keep `before-v*.sqlite3` forever.

---

## Runbook 3 — Restore (recovery)

See `docs/BACKUP_AND_RECOVERY.md` and `docs/ROLLBACK.md`. Summary:

1. Stop all processes holding DBs.
2. `health --json --with-backups` + `doctor --json` → record statuses.
3. Pick verified backup (`before-v28.sqlite3` or daily `.backup-*`).
4. `StateStorage(src, create=False).backup_to(new_private_path, confirmed=True)` to **new path**.
5. Verify `AI_EARN_STATE_DB=new_path health --json` is `ok`.
6. Swap env var, re-run health/doctor.

**Windows local first:** Use `Join-Path $HOME "AiEarn-Private\..."` and `create_private_directory` helper for new path (fixed NTFS drive, no reparse points).

Stale copy hazard: restored copy lacks later `operation_key`s → second lease possible. Do not auto-replay; use `state_recovery_comparison.compare_snapshot` for audit counts only.

---

## Runbook 4 — Configuration & secret rotation

**Env vars (process only, never `.env`):**

| Var | Allowed | Rotation |
|---|---|---|
| `AI_EARN_ENV` | `development`/`test`/`production` | Set at process start; `production` requires hardening review |
| `AI_EARN_LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | Production must not be `DEBUG` |
| `AI_EARN_AUTH_DB` | Absolute path in private dir | New private dir → new DB |
| `PAYMENT_WEBHOOK_SECRET` | 32..128 bytes, per provider/mode | Generate `secrets.token_urlsafe(32)`, validate via `secrets.validate_secret`, update provider config |

**Rotation:**

- Password: login → `R` → `YES` → new password (old code invalidated).
- Recovery code: `python -m ai_earn auth recover` consumes old, displays new once.
- Webhook secret: generate new, update `ProviderConfig`, keep old for 24h replay window if needed, then delete old.

**Secret scan before push:** `python -m tools.security_check` (checks `BEGIN PRIVATE KEY`, `sk-`, `.env` presence).

---

## Runbook 5 — Migration (upgrade)

See `docs/MIGRATION.md`. Short form:

```sh
python -m ai_earn health --json --with-backups  # must be ok
python -c "from pathlib import Path, os; from ai_earn.state_storage import StateStorage; StateStorage(Path(os.environ['AI_EARN_STATE_DB']), create=False).backup_to(Path(os.environ['AI_EARN_STATE_DB']+'-pre-mig'), confirmed=True)"
python -c "from pathlib import Path, os; from ai_earn.state_storage import StateStorage; StateStorage(Path(os.environ['AI_EARN_STATE_DB']), create=False).migrate_to_v28(Path(os.environ['AI_EARN_STATE_DB']).parent/'before-v28.sqlite3', confirmed=True)"
python -m ai_earn health --json --with-backups
python -m ai_earn doctor --json
```

If `migrate_to_vN` fails, it rolls back — restore from `before-vN.sqlite3` per Rollback runbook.

---

## Runbook 6 — Data export (privacy)

```sh
python -m ai_earn data export   # interactive: enter username, hidden password, confirm
# or API:
python -c "from pathlib import Path, os; from ai_earn.data_export import export_user_data; print(export_user_data(token, auth_db=Path(os.environ['AI_EARN_AUTH_DB']), state_db=Path(os.environ['AI_EARN_STATE_DB']), destination=Path(os.environ['AI_EARN_STATE_DB']).parent/'export.json'))"
```

Export is owner-scoped, redacted, 50 MB cap, written to same private dir `0o600`. Keep private; do not share.

---

## Runbook 7 — Account deletion (GDPR)

Two-phase, safeguards: password re-entry + phrase `DELETE <8>`.

```sh
python -m ai_earn data delete   # Step 1 prints backup_path + phrase, Step 2 asks password + phrase
```

Details: `ai_earn/account_deletion.py` `prepare_deletion_backup` → `confirm_deletion`. Deletes only **your** rows across 40+ tables, revokes sessions, removes account, preserves backup. Rate-limited. Verify post-condition: `health` → `auth: ok but account_count 0` and `state: ok`.

---

## Runbook 8 — Incident & error reporting

Structured logs (`ai_earn/structured_logging.py`) emit JSON to stderr; error reporting (`ai_earn/error_reporting.py`) returns `error_id` (8 hex) + `code` without secrets.

On error: note `error_id` from stderr/user message, run `doctor --json`, capture `health --json`, and create a ticket with **redacted** log lines only.

**Never paste** `AI_EARN_*_DB` values, passwords, tokens, or recovery codes.

---

## Windows local deployment (first)

See `docs/DEPLOYMENT.md` § Local Windows. Summary PowerShell:

```powershell
$private = Join-Path $HOME "AiEarn-Private"
python -c "from pathlib import Path; from ai_earn.windows_security import create_private_directory; create_private_directory(Path(r'$private'))"
$env:AI_EARN_AUTH_DB = Join-Path $private "auth.sqlite3"
$env:AI_EARN_STATE_DB = Join-Path $private "state.sqlite3"
python -m ai_earn auth register   # hidden password, save code outside private dir
python -m ai_earn tracker init    # type YES
python -m ai_earn tracker open    # use 1-4 etc
python -m ai_earn health --json --with-backups
python -m ai_earn doctor --json
python -m ai_earn dashboard snapshot # interactive, creates dashboard-snapshot-*.json beside state DB
python -m http.server 8000 --bind 127.0.0.1  # open http://127.0.0.1:8000/dashboard/app.html → Load snapshot
```

Must be on fixed NTFS drive with persistent ACLs, no reparse points, trusted ancestors.

---

## Production deployment (optional, not approved)

See `docs/DEPLOYMENT.md` § Production architecture. Key: **never expose SQLite over network** — local SQLite is single-user. Production would require Postgres-per-user or SQLite-vault with `127.0.0.1` app tier, `Secure; HttpOnly; SameSite` cookies, CSRF, DB-backed rate limit, `health --json` gate, and 11-item `PRODUCTION_READINESS.md` sign-off. `production_ready` stays `false` until reviews pass.
