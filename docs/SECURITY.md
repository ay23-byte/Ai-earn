# Security and recovery: experimental offline accounts

## Scope and trust boundary

No network listener, browser, public endpoint, upload facility, customer data or financial integration exists. The auth component is a local prototype, not a server or OS security boundary. Anyone who can control the process or read/write its database can attack credentials, alter records, or bypass account authorization. Do not use it for sensitive customer or financial data. HTTP cookies, CSRF, security headers and upload controls are not applicable to this offline CLI; they are mandatory design and test work before adding a web interface. No claim of a completed security audit is made.

On POSIX, an explicitly configured absolute database path must be inside a current-user-owned parent directory with no group/other mode permissions. The database must be an owner-owned regular file with no group/other mode permissions; final-component symlinks are rejected. New files use mode 0600. These checks are not protection from privileged users, a compromised OS account, malicious same-user processes, unsafe ancestor directories or all ACL/filesystem configurations. The CLI rejects production mode. **Authentication is disabled on Windows/non-POSIX** pending completion and review of the Windows authentication and terminal tests. Do not remove that restriction merely to make the command run. The status command remains available.

## Authentication behavior

- Usernames are 3–32 lowercase ASCII letters, numbers or underscores. Passwords are 12–256 characters and stored as PBKDF2-HMAC-SHA256 with random 16-byte salts and 600,000 iterations. This is not a guarantee against offline guessing of weak passwords; use a unique long password.
- Registration, login and recovery have persistent per-username and global attempt limits in a 15-minute window. Successful attempts also count. Excess attempts are rejected. The controls are local to one database, not suitable as a network abuse defense.
- Login creates a random 256-bit bearer session; only its SHA-256 digest is stored. The CLI holds the token in process memory, logs out after the interactive session, and does not print it. Sessions expire in 30 minutes and can be revoked. Session validity and rate-limit timestamps are checked after acquiring the database transaction lock, rather than before a possible wait. Expiration uses the system wall clock; a substantially incorrect or manipulated clock can affect expiry and limits. Database access is required for every profile check, which derives ownership from the validated session; cross-account profile access is rejected.
- A random single-use recovery code is displayed once at registration and replaced on use. Store it securely, not in chat, shell history, a screenshot, a log or the repository. Recovery revokes active sessions. If delivery is interrupted or a code is lost but the password still works, sign in and explicitly confirm replacement (`R`, then `YES`); the old code is invalidated and only a digest of the new code is stored. Replacement checks the active session and account ownership, is rate-limited to five attempts per account and 20 globally per 15-minute window, and creates an audit event. If display fails, sign in and repeat after the limit permits. There is no email, support bypass, or recovery if both password and code are lost. Codes do not expire until used; guard them like passwords. Secret prompts reject non-interactive input and refuse getpass's echoed-input fallback.
- Audit entries record bounded, fixed event names, account IDs where appropriate and timestamps, not passwords, tokens or recovery codes. Repeated denied/rate-limited events are coalesced in a window. This is not a tamper-proof audit log. Restrict filesystem access and monitor independently if a later phase adds real sensitive operations.

## Backup and restore

No schema migration is implemented; only an empty database can be initialized to auth schema version 1. Existing unknown versions or nonempty unversioned databases are rejected. Never overwrite, delete or migrate an existing database to make startup work. Keep its original and investigate first.

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
AuthStorage(source)  # Checks source ownership and permissions.
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
store = AuthStorage(path)
with store.transaction() as conn:
    now = int(time.time())
    conn.execute('UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL', (now,))
    conn.execute('INSERT INTO auth_audit(occurred_at,event) VALUES(?,?)', (now, 'restore_sessions_revoked'))
PY
```

Then rotate all restored passwords/recovery codes using the recovery workflow: a stale backup may resurrect previously revoked sessions and older credentials. If you cannot validate the restoration and rotate affected credentials, do not use the restored database; seek help. Reconcile any future business data separately—none exists now.

## Incident response

Stop using the component, restrict access to the database and backups, preserve evidence without exposing secrets, and assess whether the OS account or recovery codes were compromised. Rotate affected credentials on a trusted device and restore only from a verified, protected backup if necessary. No centralized emergency-stop service or network integration exists yet.

## Windows ACL follow-up: unverified, not enabled

`ai_earn/windows_security.py` contains an experimental Win32 owner/DACL verifier and a function to create a new protected test directory without modifying an existing directory. It attempts to allow only the current user and SYSTEM, rejects reparse points and unexpected ACLs, and requires a fixed local drive with persistent ACLs. The user reported that the real Windows-specific tests passed on their laptop; the sandbox has not independently executed those APIs on Windows. The user also reported completing the disposable terminal flow and confirmed that password and recovery-code input were hidden; a reviewed phase decision remains outstanding. `AuthStorage` continues to reject Windows unless a test monkeypatch explicitly overrides its internal gate; normal users must not override it. See [WINDOWS_TESTING.md](WINDOWS_TESTING.md) for safe synthetic tests. A successful platform-policy unit test on Linux is not evidence that Windows ACL creation, inspection, SQLite sidecars, or real terminal behavior are secure. A real Windows review, rerun of regressions and explicit phase decision are still required before Windows authentication can be enabled.
