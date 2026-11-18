# Backup & Recovery Release Validation

## VERIFIED (disposable data)

Backup creation, integrity checks, checked pre-migration backups,
corruption detection, interrupted-operation recovery, and restore drills
are covered by the suite (`tests/test_recovery_drill.py`,
`tests/test_state_crash_recovery.py`, backup/reopen assertions across
service tests) — all passing in the 1,037-test run. Backups contain no
credentials (secrets are environment-only and never persisted).

## Recovery procedure (operator)

1. Stop the application. Copy the current DB aside (never overwrite first).
2. Verify backup integrity (`ai_earn doctor`/health checks) before restore.
3. Restore the checked backup to the state path; reopen and run health.
4. Confirm owner login + record counts before resuming writes.

## Limitations

Drills use controlled crash points on disposable data — not power-loss or
hostile same-user tampering evidence. Destructive restore must never be
tested against real production data.
