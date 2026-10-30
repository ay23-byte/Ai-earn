# Architecture decisions

## ADR-001: Small offline standard-library foundation (Phase 1)

**Status:** Adopted for the current foundation only, 2026-10-30.

**Context:** Phase 0 found no existing application. The user has Python experience, a Windows laptop and a limited budget. Security and business workflows have later mandatory gates. Starting a public service or choosing a provider before these gates would create unnecessary risk and assumptions.

**Decision:** Start with Python 3.11+ standard library, a command-line entrypoint, validated environment configuration, fixed structured events and automated unittest tests. The code uses no third-party runtime or test dependencies, database, server, background jobs or external services. No dependency lockfile is necessary for an empty third-party dependency set; any future dependency must be reviewed, locked and installed reproducibly. Python itself is a prerequisite, not bundled or pinned by this repository.

**Alternatives:** Node plus a frontend/server could ultimately support a dashboard, but adds an ecosystem and public-server risk before security controls exist. A Python web framework may be useful later but would add a dependency and interface before requirements and security are implemented. A local CLI supplies a verifiable low-cost increment without precluding a web UI.

**Consequences:** The CLI cannot earn money, provide a dashboard or store records. Windows commands are documented but not yet verified on Windows. Production labels do not imply production isolation or readiness. Later phases must reconsider interfaces, authentication, database, dependency management and deployment against their actual needs, with a recorded plan and tests.

## Database and migrations

At the Phase 1 checkpoint no database or schema existed. Phase 2 now introduces the auth-only schema described below. Before any schema upgrade or broader business database, document the database location and permissions, versioned migration and rollback strategy, transaction and ownership boundaries, and a backup/restore procedure; test restoration against a copy. Never overwrite or delete existing data without authorization and a verified recovery plan.

## ADR-002: Auth-only local SQLite prerequisite (Phase 2)

**Status:** Experimental, Phase 2 incomplete. An auth-only versioned SQLite database is a necessary dependency for persistent authentication, throttling, session revocation and audits. It does not implement Phase 3's business state machines, migrations, ledger or task durability. The database path is explicitly supplied; the application creates no database during the `status` command. The schema initializes only an empty file and refuses unknown versions; no data is automatically migrated or deleted.

Python's standard library implements PBKDF2-HMAC-SHA256, cryptographic randomness, SQLite and hidden terminal input without introducing a dependency. POSIX owner/mode checks are enforced. User-reported Windows ACL tests, hidden-input confirmation and an isolated no-runtime-gate-override candidate run now support preparing an experimental Windows development path. Per-operation Windows ACL checks remain required; this latest unpatched checkout needs Windows verification before Phase 2 exit. Trusted ancestors and a trusted OS account are assumptions, not enforced OS isolation. A future web interface requires a new reviewed design for session cookies, CSRF, transport, access control, deployment and audit.
