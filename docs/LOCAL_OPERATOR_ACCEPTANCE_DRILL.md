# Local Operator Acceptance and Recovery Drill

**Purpose:** provide repeatable, human-run acceptance evidence for the supported Windows-local workflow. This is a test procedure, not a production approval. It must use a fresh disposable checkout and synthetic account only.

**Baseline:** PR #74 merge commit `2d73d1c0e9d975886492fa835d943cd5d1351731` (includes PR #73 and the previously merged workspace, finance and safety increments), schema v30. Record the exact candidate commit used for the drill. This baseline update does not mean the human-run drill has been performed. The optional local Ollama provider is not required for core acceptance; if exercised, use a local model and synthetic prompt data only.

## Safety rules

- Do not point this drill at an existing personal or customer database.
- Use a new private directory on a trusted local NTFS fixed drive. Do not use OneDrive, a network share, removable media, or a shared folder.
- Do not paste passwords, recovery codes, database contents, environment dumps, or screenshots containing personal data into an issue or chat.
- Never run destructive cleanup against a path you did not create for this drill.
- Keep the live-payment gate closed. Do not configure a real payment provider or submit a real payment.
- Stop and record a failure if a privacy, ACL, integrity, or backup check fails. Do not bypass a gate.

## 1. Record the test environment

Record only non-secret information:

| Field | Value |
|---|---|
| Candidate commit | |
| Windows edition/build | |
| Python version (3.11+) | |
| Local fixed drive / NTFS confirmed | |
| Test date and operator | |
| Result | NOT RUN / PASS / FAIL |

## 2. Create an isolated disposable environment

Open PowerShell in a fresh checkout:

```powershell
git clone https://github.com/ay23-byte/Ai-earn.git Ai-earn-acceptance
cd Ai-earn-acceptance
git checkout <candidate-commit>
python --version
python -m venv .venv

$private = Join-Path $HOME ("AiEarn-Acceptance-" + [guid]::NewGuid().ToString("N"))
.\.venv\Scripts\python.exe -c "from pathlib import Path; from ai_earn.windows_security import create_private_directory; create_private_directory(Path(r'$private'))"

$env:AI_EARN_ENV = "development"
$env:AI_EARN_LOG_LEVEL = "INFO"
$env:AI_EARN_AUTH_DB = Join-Path $private "auth.sqlite3"
$env:AI_EARN_STATE_DB = Join-Path $private "state.sqlite3"
```

Confirm the directory was created successfully and is private. If the helper refuses the path, stop and choose a different trusted local path; do not weaken ACL checks.

Register a synthetic local account and store the one-time recovery code securely outside the test directory:

```powershell
.\.venv\Scripts\python.exe -m ai_earn auth register
.\.venv\Scripts\python.exe -m ai_earn tracker init
.\.venv\Scripts\python.exe -m ai_earn health --json --with-backups
.\.venv\Scripts\python.exe -m ai_earn doctor --json
```

Expected: health is `ok:true`; doctor reports the auth and state stores healthy; the schema is current; the fresh v30 backup chain reports 29 expected backups. Record actual output status and counts, not secrets.

## 3. Run automated Windows checks

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_windows_security tests.test_windows_auth -v
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m tools.windows_candidate_smoke
```

Expected: Windows-specific tests execute rather than being skipped; the full suite exits successfully; the terminal smoke completes registration, login, recovery and a second login using synthetic credentials. Record totals, skips and failures exactly. If the smoke tool is absent from the candidate, record NOT AVAILABLE rather than substituting an unreviewed script.

## 4. Exercise the live local dashboard

Start the authenticated server in the same PowerShell process environment:

```powershell
.\.venv\Scripts\python.exe -m ai_earn serve
```

Open `http://127.0.0.1:8765/` in a browser. Verify:

- [ ] The page loads only from loopback; no LAN/public address is used.
- [ ] Sign-in succeeds with the synthetic account; sign-out invalidates the session.
- [ ] Refreshing the page preserves an authenticated session only while its session remains valid.
- [ ] Dashboard metrics and resource lists load from the local account database, not fabricated demo values.
- [ ] Create a synthetic customer; reopen it and confirm the same owner can see it.
- [ ] Add a customer note and requirement; refresh and confirm both persist.
- [ ] Create a service and proposal; exercise allowed state transitions only.
- [ ] For an accepted customer-linked proposal, create and view its order; duplicate conversion is rejected.
- [ ] Create and issue a local invoice; verify this is only an invoice record/PDF, not evidence of payment.
- [ ] Create a synthetic expense; refresh and confirm it persists in the Expenses workspace and remains owner-scoped.
- [ ] Create a sandbox payment linked to an issued invoice/order using the live selectors; verify its status and unverified flag.
- [ ] Confirm sandbox/provider-backed records cannot be marked verified through the dashboard or workspace API. Never initiate real payments.
- [ ] Confirm manual payment records can only be explicitly verified after the record is settled, and only after the operator checks external evidence outside this application.
- [ ] Sign out and verify protected API requests no longer return account records.

Stop the server with Ctrl+C. Do not use a tunnel, reverse proxy, LAN bind, or public internet exposure.

## 5. Keyboard and accessibility review

Use a real browser, not only the jsdom/axe automated checks:

- [ ] Navigate all controls using keyboard only; focus is visible and follows a logical order.
- [ ] Skip link moves focus to main content.
- [ ] Forms have programmatic labels and required fields are announced.
- [ ] Status, loading, empty and error messages are announced appropriately.
- [ ] At 320px, 768px and 1024px widths, no essential control is clipped or unreachable.
- [ ] Check text and control contrast in both light and dark themes with browser accessibility tooling.
- [ ] Complete a screen-reader pass with NVDA on Windows for sign-in, customer details, forms, status changes and error states.

Record browser, screen reader/version, viewport sizes, issues and evidence. A passing automated axe/jsdom result is not a substitute for this review.

## 6. Disposable backup and restore drill

Keep the synthetic environment. Do not overwrite either active database. Create a new destination that does not already exist:

```powershell
$restoreCopy = Join-Path $private "state-restore-drill.sqlite3"
.\.venv\Scripts\python.exe -c "from pathlib import Path; import os; from ai_earn.state_storage import StateStorage; StateStorage(Path(os.environ['AI_EARN_STATE_DB']), create=False).backup_to(Path(r'$restoreCopy'), confirmed=True)"

$originalState = $env:AI_EARN_STATE_DB
$env:AI_EARN_STATE_DB = $restoreCopy
.\.venv\Scripts\python.exe -m ai_earn health --json
.\.venv\Scripts\python.exe -m ai_earn doctor --json
$env:AI_EARN_STATE_DB = $originalState
```

Expected: the backup call succeeds without overwriting; health and doctor validate the copied state database. Confirm the original state path is restored in the shell. This is a disposable state-store restore check, not a full disaster-recovery proof for auth + state together.

If any command fails, preserve the disposable files for diagnosis and stop. Do not delete the original database or attempt repair in place.

## 7. Evidence record

| Check | Result (PASS / FAIL / NOT RUN) | Evidence / issue reference |
|---|---|---|
| Private directory and ACL checks | | |
| Registration / login / recovery smoke | | |
| Windows-specific test suite | | |
| Full unittest suite | | |
| Health + doctor | | |
| Live dashboard sign-in / sign-out | | |
| CRM notes and requirements persistence | | |
| Proposal → order → invoice workflow | | |
| Expense create / list / persistence | | |
| Payment linking and unverified sandbox boundary | | |
| Loopback-only server verified | | |
| Keyboard / contrast / screen-reader review | | |
| Backup to new path + restored-copy health | | |
| Payment safety boundary confirmed | | |

**Operator conclusion:** NOT ACCEPTED until every required row has a recorded result and all failures are resolved or explicitly accepted by the accountable owner.

**Important:** passing this local drill does not constitute an independent penetration test, hosted SaaS readiness, live-payment authorization, legal/accounting compliance, or a guarantee of income. Independent security review and explicit human release sign-off remain separate gates.
