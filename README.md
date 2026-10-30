# Ai-earn

Ai-earn is in early development. It currently contains an offline status command, an **experimental local authentication component**, and internal Phase 3 storage, opportunity-draft, task-plan, scheduling-queue, local task-review, queue/workflow-association and opt-in paired start/cancellation, lease-aware review, lease recovery and opt-in local completion, order-estimate, invoice-draft, unverified-payment-placeholder and recovery-comparison experiments for disposable tests. There is no dashboard, opportunity tracker, customer data, automation or payment processing. It makes no income claims and is not production-ready. Phase 3 is in progress; see [phase status](docs/PHASE_STATUS.md).

**Windows:** the user reported the experimental offline authentication path passed its Windows tests at `72241cc`. Phase 2 passed with documented limitations for this offline increment only; it is not production-ready. Use only disposable test credentials, and do not store customer or financial data.

## Setup and tests

Python 3.11+ is required; no third-party packages are used. From the repository root:

Windows PowerShell:

```powershell
python --version  # Verify Python is 3.11 or newer.
python -m venv .venv  # Create only if you do not already have a working .venv.
.\.venv\Scripts\python.exe -m ai_earn status
.\.venv\Scripts\python.exe -m unittest discover -v
```

POSIX (macOS/Linux):

```sh
python3 -m venv .venv
.venv/bin/python -m ai_earn status
.venv/bin/python -m unittest discover -v
```

A compatible `python -m ai_earn status` also runs directly from the repository root. It prints a JSON response and a fixed JSON event, with `production_ready: false`. The status describes available components, not whether a database exists. No opportunity, customer or income data is created.

## Experimental local accounts

POSIX and Windows are supported by the development code; other platforms remain disabled. The missing-database fix passed user-reported Windows tests; use only disposable test data. Use only on a trusted personal device and operating-system account; account authorization is **not** an OS sandbox. Anyone able to control the process or read/modify the database can attack or bypass these protections. Do not expose this component over a network or use it with sensitive data.

On POSIX, first create a new private directory, then configure an **absolute** path to a database inside it (the directory must be owned by the current user and have no group/other access; the app checks these restrictions):

```sh
mkdir -m 700 "$HOME/.ai-earn-private"
export AI_EARN_AUTH_DB="$HOME/.ai-earn-private/auth.sqlite3"
python3 -m ai_earn auth register
python3 -m ai_earn auth login
# Only if needed: python3 -m ai_earn auth recover
```

For Windows, the Phase 2 test-only commands are in [docs/WINDOWS_TESTING.md](docs/WINDOWS_TESTING.md); the Phase 3 verification procedure is in [docs/WINDOWS_PHASE3_TESTING.md](docs/WINDOWS_PHASE3_TESTING.md). The Windows helper can create a **new** private directory with a protected ACL; it never modifies an existing directory. It requires a fixed local drive with persistent ACLs and rejects reparse points. Never select a directory under untrusted ancestors. Use only disposable test data.

If the directory already exists, do not overwrite or change it blindly: inspect its owner and permissions before use. Registration can create a new private SQLite file if needed; login and recovery require an existing initialized database. If a previously used database is missing, do not register at that path: investigate or restore a backup. The app rejects symlinks, unsafe permissions and unsupported schemas. Never place it in the repository. Commands require a real terminal: passwords and recovery codes are entered with hidden input, never as arguments. Registration displays **one recovery code once**; save it securely outside the app and do not share it. Recovery consumes the code, revokes active sessions and displays a replacement once. If a code is lost or its display is interrupted **but you still know your password**, sign in, enter `R` at the sign-out prompt, and explicitly confirm `YES` to replace it. Replacement invalidates the old code; save the new code securely. If both your password and recovery code are lost, there is no automatic recovery channel. Closing or interrupting a logged-in CLI logs out when possible; sessions also expire after 30 minutes.

Login, registration and recovery attempts are rate-limited. Too many attempts produce a temporary refusal; wait 15 minutes before trying again. This is a local prototype, not an Internet-facing rate-limit system. For database backup, restoration, permissions and limitations, read [docs/SECURITY.md](docs/SECURITY.md) first. Restoring stale backups can restore old passwords or recovery codes and previously active sessions.

## Configuration

Configuration comes only from process environment variables. `.env.example` is a reference and is not automatically loaded. Never put real secrets in the example or source control.

| Variable | Default | Allowed values |
|---|---|---|
| `AI_EARN_ENV` | `development` | `development`, `test`, `production` |
| `AI_EARN_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `AI_EARN_AUTH_DB` | None | Absolute path to the auth-only database in an owner-only directory; required for auth |

Invalid configuration is rejected without echoing values. Environment labels do not isolate data; explicitly choose separate database paths for development and testing. The auth CLI refuses the `production` label. No environment is approved for public deployment.

## Troubleshooting

- `No module named ai_earn`: run from the repository root with Python 3.11+.
- Invalid configuration: check the named variable against the table; unset it to restore the default. Do not paste secrets into tickets.
- Authentication storage errors: confirm the path is absolute, the parent directory is private and owned by you, and the database is an owner-only regular file. Do not delete or replace an existing database as a troubleshooting step. Windows development support is experimental and not approved for production or real sensitive data; use only disposable test data.
- The `.venv` directory is ignored by Git; recreate it if its Python installation breaks, but preserve your own files.

Details: [testing](docs/TESTING.md), [architecture](docs/ARCHITECTURE.md), [threat model](docs/THREAT_MODEL.md), and [security and recovery](docs/SECURITY.md).
