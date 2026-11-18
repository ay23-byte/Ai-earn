# State schema migrations

State storage uses SQLite schema versions 1–28. `ai_earn.state_storage.StateStorage` remains the compatibility and safety boundary: it validates the opened database, acquires the write transaction, makes and verifies a non-overwriting backup, invokes exactly one version migration, then checks the new schema and integrity before commit.

Each upgrade's immutable DDL/index definitions and apply operation live in `ai_earn/migrations/vNN.py` (`v02.py` through `v28.py`). Each module exports `apply(conn)` and its versioned schema constants. Keep migration definitions append-only: a released migration must not be edited to change the schema for an already-upgraded database; add a new version instead.

`state_storage._apply_vN` remains as a compatibility alias to the corresponding migration module's `apply` function because tests and controlled fault-injection tools patch these names. The migration split does not change the migration order, schema version, `APPLICATION_ID`, backup policy, confirmation requirement, or rollback behavior.

Run the focused migration and recovery tests after editing a migration:

```powershell
python -m unittest tests.test_migration_modules tests.test_state_migration tests.test_state_crash_recovery tests.test_recovery_drill -v
python -m unittest discover -v
```

All tests use isolated disposable databases. Never test destructive recovery or migration behavior on a user's real database.
