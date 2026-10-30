# Security and recovery: experimental offline accounts

## Scope and trust boundary

No network listener, browser, public endpoint, upload facility, customer data or financial integration exists. The auth component is a local prototype, not a server or OS security boundary. Anyone who can control the process or read/write its database can attack credentials, alter records, or bypass account authorization. Do not use it for sensitive customer or financial data. HTTP cookies, CSRF, security headers and upload controls are not applicable to this offline CLI; they are mandatory design and test work before adding a web interface. No claim of a completed security audit is made.

On POSIX, an explicitly configured absolute database path must be inside a current-user-owned parent directory with no group/other mode permissions. The database must be an owner-owned regular file with no group/other mode permissions; final-component symlinks are rejected. New files use mode 0600. These checks are not protection from privileged users, a compromised OS account, malicious same-user processes, unsafe ancestor directories or all ACL/filesystem configurations. The CLI rejects production mode. Experimental Windows development authentication is enabled based on user-reported ACL, terminal and candidate tests. The user reported that the unpatched checkout including the missing-database fix passed its Windows tests at `72241cc`. The offline Phase 2 increment passed with documented limitations, not production approval. Use only disposable test data; other non-POSIX platforms remain disabled. Windows checks reject reparse-point ancestors but do not verify that all ancestors are trusted or prevent all path-swap races. Use only a trusted personal device and trusted ancestor directories; the checks are not an OS isolation boundary.

## Authentication behavior

- Usernames are 3–32 lowercase ASCII letters, numbers or underscores. Passwords are 12–256 characters and stored as PBKDF2-HMAC-SHA256 with random 16-byte salts and 600,000 iterations. This is not a guarantee against offline guessing of weak passwords; use a unique long password.
- Registration, login and recovery have persistent per-username and global attempt limits in a 15-minute window. Successful attempts also count. Excess attempts are rejected. The controls are local to one database, not suitable as a network abuse defense.
- Login creates a random 256-bit bearer session; only its SHA-256 digest is stored. The CLI holds the token in process memory, logs out after the interactive session, and does not print it. Sessions expire in 30 minutes and can be revoked. Session validity and rate-limit timestamps are checked after acquiring the database transaction lock, rather than before a possible wait. Expiration uses the system wall clock; a substantially incorrect or manipulated clock can affect expiry and limits. Database access is required for every profile check, which derives ownership from the validated session; cross-account profile access is rejected.
- A random single-use recovery code is displayed once at registration and replaced on use. Store it securely, not in chat, shell history, a screenshot, a log or the repository. Recovery revokes active sessions. If delivery is interrupted or a code is lost but the password still works, sign in and explicitly confirm replacement (`R`, then `YES`); the old code is invalidated and only a digest of the new code is stored. Replacement checks the active session and account ownership, is rate-limited to five attempts per account and 20 globally per 15-minute window, and creates an audit event. If display fails, sign in and repeat after the limit permits. There is no email, support bypass, or recovery if both password and code are lost. Codes do not expire until used; guard them like passwords. Secret prompts reject non-interactive input and refuse getpass's echoed-input fallback.
- Audit entries record bounded, fixed event names, account IDs where appropriate and timestamps, not passwords, tokens or recovery codes. Repeated denied/rate-limited events are coalesced in a window. This is not a tamper-proof audit log. Restrict filesystem access and monitor independently if a later phase adds real sensitive operations.

## Backup and restore

No schema migration is implemented; only an empty database can be initialized to auth schema version 1. CLI login and recovery require an existing initialized database and do not intentionally recreate a missing one; transactions open in SQLite read/write-existing mode. Registration and the default `AuthStorage` constructor can still initialize a missing or empty database, so do not register at a missing path that previously held data: stop and investigate or restore a backup. Existing unknown versions or nonempty unversioned databases are rejected. Never overwrite, delete or migrate an existing database to make startup work. Keep its original and investigate first.

Before attempting any future upgrade, stop all application access. Use SQLite's `Connection.backup` API to copy the live database into a **new**, separately protected file; do not simply copy a potentially active database and do not overwrite an existing backup. The automated suite exercises backup and login against a separate temporary copy, but no disaster-recovery rehearsal with real user data has been performed. Protect backups as sensitive credentials, keep copies separately, verify them, and follow applicable retention requirements. Python backup sketch (POSIX only; set both environment variables to absolute paths in existing owner-only directories, with the destination not already present):

```python
import os, sqlite3, stat
from contextlib import closing
from pathlib import Path
from ai_earn.auth_storage import AuthStorage, SCHEMA_VERSION
source = Path(os.environ["AI_EARN_AUTH_DB"])
destination = Path(os.environ["AI_EARN_BACKUP_DB"])
if not source.is_absolute() or not source.is_file():
    raise RuntimeError("Source missing; do not create a new database")
# Check read-only first: never initialize an empty or unrelated database as a backup source.
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as check:
    if check.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError("Unsupported source schema")
AuthStorage(source, create=False)  # Requires an initialized source and checks ownership and permissions.
if not destination.is_absolute():
    raise RuntimeError("Backup destination must be absolute")
parent = destination.parent.lstat()
if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) & 0o077):
    raise RuntimeError("Backup directory must be owner-only")
fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
```

If interrupted or backup fails, treat the newly created file as incomplete, not a valid backup. Do not delete existing files automatically. To restore, stop the application and preserve the damaged original. Run the same sketch with `AI_EARN_AUTH_DB` set to the verified backup and `AI_EARN_BACKUP_DB` set to a **new** owner-only restore path. Then point `AI_EARN_AUTH_DB` to the new restore path. Before allowing use, revoke all restored sessions in that copy; the following command modifies that explicitly selected database and must not run against the original or the backup:

```sh
python3 - <<'PY'
import os, sqlite3, time
from contextlib import closing
from pathlib import Path
from ai_earn.auth_storage import AuthStorage, SCHEMA_VERSION
path = Path(os.environ['AI_EARN_AUTH_DB'])
if not path.is_absolute() or not path.is_file():
    raise RuntimeError('Restore copy missing; refusing to create a database')
with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as check:
    if check.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        raise RuntimeError('Unsupported restore schema')
store = AuthStorage(path, create=False)
with store.transaction() as conn:
    now = int(time.time())
    conn.execute('UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL', (now,))
    conn.execute('INSERT INTO auth_audit(occurred_at,event) VALUES(?,?)', (now, 'restore_sessions_revoked'))
PY
```

Then rotate all restored passwords/recovery codes using the recovery workflow: a stale backup may resurrect previously revoked sessions and older credentials. If you cannot validate the restoration and rotate affected credentials, do not use the restored database; seek help. Reconcile any future business data separately—none exists now.

## Incident response

Stop using the component, restrict access to the database and backups, preserve evidence without exposing secrets, and assess whether the OS account or recovery codes were compromised. Rotate affected credentials on a trusted device and restore only from a verified, protected backup if necessary. No centralized emergency-stop service or network integration exists yet.

## Windows ACL follow-up: experimental, offline verification

`ai_earn/windows_security.py` contains an experimental Win32 owner/DACL verifier and a function to create a new protected test directory without modifying an existing directory. It attempts to allow only the current user and SYSTEM, rejects reparse points and unexpected ACLs, and requires a fixed local drive with persistent ACLs. The user reported that the real Windows-specific tests passed on their laptop; the sandbox has not independently executed those APIs on Windows. The user also reported completing the disposable terminal flow and confirmed that password and recovery-code input were hidden; that earlier harness used a gate override. The latest Windows suite and disposable smoke were reported passing without a gate override at `72241cc`, including the missing-database tests. The automated smoke mocked CLI secrets. The offline phase passed with documented limitations; see [WINDOWS_TESTING.md](WINDOWS_TESTING.md). Linux ACL-policy tests are not Win32 verification, and neither reported tests nor phase exit constitute a security audit. Do not use real or sensitive data.

## Phase 3 state-storage boundary (experimental)

The new internal state store is separate from the auth database. It authenticates no caller and is not safe to expose directly through a UI, API or agent. Future business operations must enforce authorization, ownership and policy independently. Do not store real customer, financial or sensitive data; use only disposable tests in a private absolute path outside the repository. Existing auth databases must remain separate and are not backed up by this feature. The store reuses Phase 2 file checks, so a trusted OS account and trusted ancestors remain essential; path-swap races are not eliminated. The user reported passing the disposable state-store tests through v3 on Windows; the new v4/v5 migrations, task plans and order drafts are unverified on Windows. These tests are not an independent audit or real-data recovery test.

The internal session-bound unit of work acquires auth before state, derives the actor from the session, and checks expiry after locking and before state commit. It serializes normal logout/recovery with an in-flight operation, but does not implement per-record authorization. Trusted application code must select and keep a fixed auth/state pairing, never expose raw connections or allow callers to substitute databases, and never reverse the lock order. The two databases do not commit atomically together: an error after state commit can make the result ambiguous, so future business writes require durable idempotency before exposure. The user reported passing unit-of-work tests on Windows; do not use real data.

The explicit v1-to-v2 migration requires confirmation and a checked pre-migration backup at a new private path, holding the source write lock across backup and migration. DDL, history and version are committed atomically; after failure preserve the original and backup and restore only to a new path. A hard crash can leave a journal requiring investigation; do not blindly delete sidecars. Old code rejects v2, so roll back code only with a compatible restored copy. No auth database is migrated. New v2 tables do not enforce business authorization, idempotency or audit integrity on their own.

Creation requires explicit confirmation and a new path. Open refuses missing, empty or unsupported schemas rather than initializing them. Backup uses a read-only source, checks identity/schema and SQLite `quick_check`, then writes and checks a consistent committed snapshot at an exclusively created new destination. Restore is the same operation from backup to another new path; preserve the original and backup. Neither replaces an existing file. Never treat a failed or interrupted copy as valid: a partial file can remain, and it is not deleted automatically. Verify permissions, paths and results before any manual cleanup; `quick_check` alone does not establish completeness, authenticity or safety. Do not back up while relying on uncommitted writes. Backup/restore tests use disposable data only, and no real recovery, business reconciliation or production readiness is claimed.

### Experimental opportunity drafts

The v2-to-v3 migration follows the same explicit backup-first rules and leaves old code unable to open v3; keep the pre-migration backup and restore it to a new file if rolling back. `OpportunityDrafts` is an internal service using a live session and owner-filtered record queries, with only draft creation, read and `draft -> archived`. It is not a web or OS security boundary; its tests were reported passed by the user on Windows. The caller must choose a trusted fixed auth/state pair; the databases are not cryptographically bound. Do not let untrusted users select database paths or receive raw connections. Audit events and owner-scoped operation keys are committed with draft mutations, but the local audit is not tamper-evident, unsuccessful access is not recorded there, and direct database access can bypass the service. Operation keys persist in plaintext: use opaque non-secret values, retain each key for retries, and never reuse a key for a different operation. Restoring a stale backup can lose newer records, audit entries and idempotency keys; reconcile before any future real use. These are disposable-data tests only, not permission to process sensitive data.

### Experimental task plans

The v3-to-v4 migration also requires explicit confirmation and a new, checked pre-migration backup. Older code cannot open v4; preserve the source and restore a suitable backup to a new path if rolling back. `TaskPlans` enforces session-derived ownership for creation, read and `planned -> cancelled`, and writes audit and owner-scoped idempotency records in the same state transaction. It has no running, completion or execution path, and must not be treated as an authorized queue or worker. Task and opportunity operation keys share an owner-scoped namespace; use distinct opaque, non-secret keys and retain them for retries. Direct database access can bypass the service and the audit is not tamper-evident. All auth/state pairing, stale backup, secret and OS-trust limitations above also apply. The new v4 migration and task-plan code have not been tested on Windows; use disposable data only.

### Experimental order drafts

The v4-to-v5 upgrade requires explicit confirmation and a new checked pre-migration backup; no file is automatically migrated. Older code cannot open v5. Preserve the original and backup, and restore a compatible backup to a new path when rolling back. A v4 backup predates v5 changes, so restoring it loses later records, audit entries and operation keys; reconcile before any future real use. Interrupted or failed backups may leave partial files and must not be trusted.

`OrderDrafts` applies live-session ownership and writes fixed audit and idempotency records with each state change. The amount is an INR-only proposed integer paise value between 0 and 1,000,000,000,000 inclusive, not payment authorization, contract, revenue or a confirmed quote. Never store real customer or financial data. The owner-scoped operation-key namespace is shared with task plans and opportunity drafts; keys are plaintext and must be opaque and non-secret. The trusted fixed auth/state pairing, separately committed databases, non-tamper-evident audit and local OS assumptions remain. V4 and v5 are unverified on Windows; no independent audit or real recovery is claimed.
