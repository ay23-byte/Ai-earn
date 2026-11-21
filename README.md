# Ai-earn

Ai-earn is a **local-first freelance workflow and small-business management application**. It helps an operator organize manually sourced opportunities, package services, prepare proposals, manage customers and projects, track delivery, issue invoices, and maintain local finance records.

It is an earning-workflow tool, not an income guarantee, autonomous client-acquisition service, hosted SaaS, or live payment gateway.

## Current capabilities

- **Opportunities:** manual and supported-connector intake, evidence and risk fields, vetting and pursuit workflow.
- **Services and portfolio:** service catalog, pricing/scope, publication and archive lifecycle.
- **Proposals:** customer-linked proposal drafting, review, send/accept/reject transitions, and idempotent operations.
- **CRM and projects:** owner-scoped customer records, notes and requirements, customer-linked projects, project task lifecycle and execution history.
- **Orders and invoices:** convert an accepted customer-linked proposal into an order; create and issue invoices with local PDF artifacts.
- **Finance:** expenses, payment records, ledger/reconciliation foundations and explicit verification fields.
- **AI assistance:** provider abstraction and draft/approval workflows; review and approve consequential actions yourself.
- **Dashboard and workspace API:** authenticated local workspace endpoints and UI, with the read-only snapshot workflow retained as a fallback.
- **Security and recovery:** local authentication, owner-scoped data access, audit/idempotency records, private database paths, integrity checks, backup and recovery tooling.

The feature set is evolving. Local task completion does not prove that an external deliverable was sent or accepted. A local invoice does not prove that money was received.

## Important safety boundaries

- **Live payments remain blocked.** Sandbox/test events are not real payment evidence or bank settlement.
- Never mark income verified from a screenshot, client-side success page, manual claim, or sandbox webhook.
- Do not store card data or expose the local app or SQLite files directly to the public internet.
- Use disposable test data until you have completed operator acceptance and obtained an independent security review.
- Keep backups; do not delete, overwrite, or reset an existing database as a troubleshooting shortcut.
- Ai-earn does not guarantee earnings or independently obtain paying clients.

See [the current release checkpoint](docs/CURRENT_RELEASE_CHECKPOINT.md), [production-readiness gate](docs/PRODUCTION_READINESS.md), [security review](docs/SECURITY_REVIEW.md), and [payments boundary](docs/PAYMENTS.md).

## Supported local setup (Windows-first)

Use a **new, private directory** for disposable data. Do not point these commands at an existing personal database unless you have reviewed the migration and backup procedures.

~~~powershell
git clone https://github.com/ay23-byte/Ai-earn.git
cd Ai-earn
python -m venv .venv

# Create a new private directory on a trusted local NTFS drive.
$private = Join-Path $HOME "AiEarn-Private-Test"
.\.venv\Scripts\python.exe -c "from pathlib import Path; from ai_earn.windows_security import create_private_directory; create_private_directory(Path(r'$private'))"

# Keep both database files inside that private directory.
$env:AI_EARN_ENV = "development"
$env:AI_EARN_LOG_LEVEL = "INFO"
$env:AI_EARN_AUTH_DB = Join-Path $private "auth.sqlite3"
$env:AI_EARN_STATE_DB = Join-Path $private "state.sqlite3"

# Register a disposable account. Save the one-time recovery code privately.
.\.venv\Scripts\python.exe -m ai_earn auth register

# Initialize only the new state database. Type YES when prompted.
.\.venv\Scripts\python.exe -m ai_earn tracker init

# Verify the private stores and migration backups.
.\.venv\Scripts\python.exe -m ai_earn health --json --with-backups
.\.venv\Scripts\python.exe -m ai_earn doctor --json

# Open the local tracker.
.\.venv\Scripts\python.exe -m ai_earn tracker open
~~~

The helper refuses unsafe or pre-existing private directories. If setup fails, stop and inspect the files; do not delete them or retry initialization blindly. The detailed runbook, including dashboard startup and recovery, is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and [docs/BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md).

Runtime application code uses the Python standard library. Development quality tools are pinned separately in requirements-dev.txt.

## Developer verification

The CI workflow tests Python 3.11 and 3.13 on Ubuntu and Windows. It runs Ruff, mypy (including strict checks on selected modules), compilation, repository security checks, dependency audit policy checks, health/doctor probes, and the full unittest suite.

Run the same core checks locally:

~~~powershell
python -m pip install -r requirements-dev.txt
python -m ruff check ai_earn tests tools
python -m mypy
python -m mypy --strict ai_earn/financial_math.py ai_earn/migrations ai_earn/postgres_adapter.py ai_earn/structured_logging.py ai_earn/connectors
python -m compileall -q ai_earn tests tools
python -m tools.security_check
python -m unittest discover -v
git diff --check
~~~

The Windows CI matrix is important for ACL-specific behavior, but it does not replace a smoke test on the operator's own Windows device. Record the exact commit, platform, test totals, skips and failures when reporting verification; do not copy historical test counts.

## Project documentation

- [Current release checkpoint](docs/CURRENT_RELEASE_CHECKPOINT.md)
- [Deployment runbook](docs/DEPLOYMENT.md)
- [Backup and recovery](docs/BACKUP_AND_RECOVERY.md)
- [Production readiness and live-payment gate](docs/PRODUCTION_READINESS.md)
- [Security review and threat boundaries](docs/SECURITY_REVIEW.md)
- [Windows testing](docs/WINDOWS_TESTING.md)
- [User guide](docs/USER_GUIDE.md)
- [Phase history](docs/PHASE_STATUS.md) — historical record; older entries may describe earlier capabilities and are not the current feature inventory.
