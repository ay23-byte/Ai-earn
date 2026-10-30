# Ai-earn

Ai-earn is in early development. It currently contains an offline status command and an **experimental local authentication component**. There is no dashboard, opportunity tracker, customer data, automation or payment processing. It makes no income claims and is not production-ready. See [phase status](docs/PHASE_STATUS.md).

**Windows:** the status command works in principle but has not been tested on Windows. Account storage is deliberately disabled on Windows and other non-POSIX platforms until Windows ACL enforcement and recovery are implemented and tested. Phase 2 is blocked on that security work; do not store customer or financial information in this prototype.

## Setup and tests

Python 3.11+ is required; no third-party packages are used. From the repository root:

Windows PowerShell:

```powershell
py -3 --version  # Verify Python is 3.11 or newer.
py -3 -m venv .venv
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

## Experimental POSIX-only local accounts

Use only on a trusted personal device and operating-system account; account authorization is **not** an OS sandbox. Anyone able to control the process or read/modify the database can attack or bypass these protections. Do not expose this component over a network or use it with sensitive data.

First create a new private directory, then configure an **absolute** path to a database inside it (the directory must be owned by the current user and have no group/other access; the app checks these restrictions):

```sh
mkdir -m 700 "$HOME/.ai-earn-private"
export AI_EARN_AUTH_DB="$HOME/.ai-earn-private/auth.sqlite3"
python3 -m ai_earn auth register
python3 -m ai_earn auth login
# Only if needed: python3 -m ai_earn auth recover
```

If the directory already exists, do not overwrite or change it blindly: inspect its owner and permissions before use. The app creates a new owner-only SQLite file if needed and rejects symlinks, unsafe permissions and unsupported schemas. Never place it in the repository. Commands require a real terminal: passwords and recovery codes are entered with hidden input, never as arguments. Registration displays **one recovery code once**; save it securely outside the app and do not share it. Recovery consumes the code, revokes active sessions and displays a replacement once. If that code is lost there is no automatic recovery channel. Closing or interrupting a logged-in CLI logs out when possible; sessions also expire after 30 minutes.

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
- Authentication storage errors: confirm the path is absolute, the parent directory is private and owned by you, and the database is an owner-only regular file. Do not delete or replace an existing database as a troubleshooting step. Authentication is disabled on Windows.
- The `.venv` directory is ignored by Git; recreate it if its Python installation breaks, but preserve your own files.

Details: [testing](docs/TESTING.md), [architecture](docs/ARCHITECTURE.md), [threat model](docs/THREAT_MODEL.md), and [security and recovery](docs/SECURITY.md).
