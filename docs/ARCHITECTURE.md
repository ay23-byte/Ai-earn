# Architecture: baseline and proposed boundaries

## Observed baseline

The repository has no running application or architecture: only a README existed at the audited commit. There are no frontend/backend routes, authentication mechanisms, data stores, workers, APIs, payment components, integrations, or deployment configuration. Consequently there is no data flow or schema to assert as implemented.

## Target boundaries (not implemented)

- A local-first interface should display only persisted, real records and clearly identify empty or unverified states.
- Application services should validate input and enforce authorization and approval policy independently of any AI model or UI. Business rules and state transitions belong server-side, not in hidden controls.
- Durable storage, ownership, transactional state changes, idempotency and audit records must be established before workflows depend on them. Keep expected, invoiced, confirmed, settled and net income distinct.
- External sources and model output are untrusted data. Connectors should have narrowly scoped credentials, bounded requests and manual fallbacks; consequential actions require explicit policy enforcement and approval.
- Payment events must be independently verified and reconciled with the provider. No browser-provided status, screenshot or unverified webhook establishes payment.

These are requirements, not evidence of implemented controls. Actual language, framework, storage and authentication choices are deferred to the gated implementation plan. Since no project exists, a new minimal application can be justified, but a broad rewrite, public deployment and live financial operations are not justified by this audit.

## Recovery boundary

This documentation-only audit changes no application or database. Use `git diff` to review changes; restore individual audit files only after confirming they contain no user edits. Future implementation must document backups, tested restores, migration reversibility, incident containment and rollback appropriate to its actual storage and deployment.

## Phase 1 foundation (historical increment)

`python -m ai_earn status` runs a local process. `ai_earn/config.py` validates the environment, `status.py` reports only implemented capabilities, `responses.py` provides success/error envelopes, `events.py` emits only fixed structured event names, and `__main__.py` handles presentation and sanitized failures. Configuration has no secret fields, and events accept no arbitrary message, exception or context. This omission-based design does not guarantee safety of unrelated future logging; every future input and log path still requires review.

At the Phase 1 checkpoint the program did not listen on a network, call external services or write application data. Environment labels did not partition resources. Phase 2 subsequently introduced the auth-only database described below; no HTTP API or migration exists, and production is explicitly not ready. See [DECISIONS.md](DECISIONS.md) and [PHASE_STATUS.md](PHASE_STATUS.md).

## Phase 2: experimental local account component

`auth_storage.py` initializes an auth-only SQLite schema and validates a private POSIX file/directory. `auth.py` handles hashed credentials, throttling, session checks, ownership, recovery and fixed audit events; `auth_cli.py` is the terminal adapter. Access checks are in the service rather than the terminal UI. These controls are testable in-process, not an OS sandbox or a network server. Windows development authentication uses Windows ACL checks; the user reported passing no-override tests and the missing-database fix at `72241cc`. Other non-POSIX platforms and production-labelled auth are disabled. Phase 2 passed only the offline development scope with documented limitations. No browser or external API exists. For the trust boundary and backup hazards, see [SECURITY.md](SECURITY.md).

## Phase 3: separate state-storage foundation (in progress)

`state_storage.py` is internal infrastructure, separate from auth storage, with a distinct SQLite application ID and version 1 bootstrap history. It explicitly creates only new files, rejects uninitialized/unknown schemas, and uses transactions plus checked, non-overwriting SQLite backup to new files; restoration is another copy to a new path. It reuses the auth component's platform-private path checks and its write-transaction helper; changes to those private helpers require regression testing both modules. There are no business records, workflow state machines, business audits, ownership checks or public interfaces yet. Opening the storage is not authorization; any future business service must independently enforce session and ownership checks. Schema upgrades are not implemented. Only disposable data is supported, and this increment has not been run on Windows.
