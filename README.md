# Ai-earn

Ai-earn is in early development. It currently contains an offline `status` command, an **experimental local authentication component**, internal Phase 3 storage with opportunity-draft, **opportunity discovery (v15 manual/connector, filtering/ranking, draft AI helpers)**, task-plan, scheduling-queue, local task-review, queue/workflow-association and opt-in paired start/cancellation, lease-aware review, lease recovery, opt-in local completion and v12 managed-pair safeguards, order-estimate, invoice-draft, unverified-payment-placeholder, draft-only coordinated withdrawal, unverified financial assertions, synthetic in-memory and durable payment-event rehearsals and recovery-comparison experiments for disposable tests, plus an **experimental offline tracker** for unverified local assertions (now 1-4 financial, 5-8 opportunities, 9-12 services/portfolio, 13-15 products, 16-18 customers, 19-21 comms, 22 ledger, 23-25 followups, 26-27 CSV, 28-30 invoices, 31-33 provider sandbox, `tracker init` v21 20 backups), a **read-only local dashboard snapshot** (`dashboard/connected.html`, no HTTP API, now includes opportunity + services/portfolio + followup + invoices + 11-concept + provider metrics), **managed-task runner** (`ManagedTaskRunner` ThreadPool lease-fenced `claim→handler→submit`/`retry`), **service catalog & portfolio** (v16 draft→published→archived, `Services`/`Portfolio` with tracker 9-12), and a **read-only `doctor` diagnostic** for private-database recovery. There is no customer data, automation, verified income ledger or payment processing. It makes no income claims and is not production-ready. Phase 3 is in progress (v15 opportunity + v16 services + v17 products/customers + v18 comms/ledger + v19 followups/CSV + v20 invoices/11-concept + v21 provider sandbox + runner experimental); see [phase status](docs/PHASE_STATUS.md).

**Windows:** the user reported the experimental offline authentication path passed its Windows tests at `72241cc`. Phase 2 passed with documented limitations for this offline increment only; it is not production-ready. Use only disposable test credentials, and do not store customer or financial data.

## Experimental manual tracker (first increment)

The user-approved smaller product is an offline tracker rather than the original full financial platform. A first terminal interface is available for **disposable test data**: it creates and lists opportunities with proposed INR amounts, advances unverified quote/contract/invoice/payment-initiated stages, and withdraws entries. **It cannot yet record a claimed payment receipt**, verify a payment or settlement, issue invoices, calculate earnings, or connect to Razorpay. It is not ready for real customer or financial data. The original Phase 3 has not passed; see [phase status](docs/PHASE_STATUS.md).

On Windows PowerShell, after pulling the change and creating `.venv`, create a **new** private directory on a trusted local drive (the helper refuses existing directories). Do not reuse this test path for existing data:

```powershell
$private = Join-Path $HOME 'AiEarn-Tracker-Test'
.\.venv\Scripts\python.exe -c "import sys; from pathlib import Path; from ai_earn.windows_security import create_private_directory; create_private_directory(Path(sys.argv[1]))" "$private"
$env:AI_EARN_AUTH_DB = Join-Path $private 'auth.sqlite3'
$env:AI_EARN_STATE_DB = Join-Path $private 'state.sqlite3'
.\.venv\Scripts\python.exe -m ai_earn auth register
.\.venv\Scripts\python.exe -m ai_earn tracker init
.\.venv\Scripts\python.exe -m ai_earn tracker open
```

Registration requests a hidden password and displays a one-time recovery code: save it privately. `tracker init` requires typing `YES`, creates a new state database and 20 non-overwriting migration backups (v2–v21), and never upgrades an existing file. If setup is interrupted, **do not delete or overwrite files automatically**; inspect the private directory and use a different new test directory or review recovery. Environment variables above last only for the current PowerShell session; set them again before opening the tracker later. The tracker requires a real terminal, does not accept passwords as arguments, and closes the session on exit where possible. Entries may be visible to anyone who can access your OS account or files; never use sensitive data. If an operation is interrupted or reports an error, inspect entries before retrying: the outcome may be ambiguous and the CLI creates a new operation key for each attempt. `tracker open` now exposes Financial (1-4), Opportunities 5-8 (v15 manual/connector, unverified, draft-ranked), Services/Portfolio 9-12 (v16 draft→published→archived, portfolio FK), Products 13-15 / Customers 16-18 (v17 draft→published→archived, lead→contacted→converted/rejected), Comms 19-21 / Ledger 22 (v18 draft→sent→archived, income/expense/refund/fee net) + Followups 23-25 + CSV 26-27 (v19 pending→completed/cancelled, customer CSV) and Invoices 28-30 (v20 draft→issued→paid/overdue/void with PDF stub hash, 11-concept ledger separates verified) + Provider sandbox 31-33 (v21 Razorpay/Stripe HMAC, sandbox only), all still unverified local drafts.

If the state database is missing or permissions look wrong, run the read-only diagnostics first:

```powershell
python -m ai_earn doctor
python -m ai_earn doctor --plan-init   # explicit, confirmation-required recovery steps
```

On POSIX use a newly created owner-only directory (`mkdir -m 700`), export the same two absolute paths, and run `python3 -m ai_earn auth register`, `python3 -m ai_earn tracker init`, then `python3 -m ai_earn tracker open`. Never replace an existing database. Windows verification of this new interface is pending.

The doctor is read-only and never creates, migrates or overwrites a file. It reports
environment validity, per-store status, schema version, account count and
actionable next steps, including the `--plan-init` recovery plan. Use
`--json` for machine-readable output.

## Local authenticated dashboard snapshot

The dashboard now has a read-only local view at `dashboard/connected.html`. It displays real, account-scoped aggregate metrics from the existing state database. It does **not** run an HTTP API or send authentication tokens to the browser.

On Windows PowerShell, first set the same private database paths you use for the tracker, then export a snapshot:

```powershell
python -m ai_earn dashboard snapshot
```

The command requires an interactive terminal, asks for the account username and hidden password, opens existing databases only, and signs out after export. It creates a new, non-overwriting `dashboard-snapshot-*.json` file beside the state database, inside that database's already-protected private directory. The snapshot contains aggregate counts and amounts only; it contains no login token, username, record titles or customer details. Treat it as private financial information and do not share it.

To open the dashboard, from the repository root run a static server bound to loopback only:

```powershell
python -m http.server 8000 --bind 127.0.0.1
```

Open `http://127.0.0.1:8000/dashboard/` and choose **Load snapshot**. Select the JSON file created by the export command. The page reads the file locally in the browser; it makes no API request and does not upload the file. Select **Clear data** to remove the displayed snapshot from browser memory. Export a new snapshot when you want refreshed metrics; older exports are never overwritten.

The page deliberately labels financial assertions, invoice drafts, payment observations and sandbox events separately. Verified income remains **Not available** because there is no verified payment ledger. This remains an experimental local-only workflow for disposable data, not a production or multi-user deployment. Do not expose the authentication component over a network or enter real customer/financial data.


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
| `AI_EARN_STATE_DB` | None | Absolute path to the state-only database in the same private directory; required for tracker/dashboard/doctor |

Invalid configuration is rejected without echoing values. Environment labels do not isolate data; explicitly choose separate database paths for development and testing. The auth CLI refuses the `production` label. No environment is approved for public deployment.

## Troubleshooting

- `No module named ai_earn`: run from the repository root with Python 3.11+.
- Invalid configuration: check the named variable against the table; unset it to restore the default. Do not paste secrets into tickets.
- Authentication storage errors: confirm the path is absolute, the parent directory is private and owned by you, and the database is an owner-only regular file. Do not delete or replace an existing database as a troubleshooting step. Windows development support is experimental and not approved for production or real sensitive data; use only disposable test data.
- The `.venv` directory is ignored by Git; recreate it if its Python installation breaks, but preserve your own files.

Details: [testing](docs/TESTING.md), [architecture](docs/ARCHITECTURE.md), [threat model](docs/THREAT_MODEL.md), and [security and recovery](docs/SECURITY.md).
