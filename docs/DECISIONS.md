# Architecture decisions

## ADR-001: Small offline standard-library foundation (Phase 1)

**Status:** Adopted for the current foundation only, 2026-10-30.

**Context:** Phase 0 found no existing application. The user has Python experience, a Windows laptop and a limited budget. Security and business workflows have later mandatory gates. Starting a public service or choosing a provider before these gates would create unnecessary risk and assumptions.

**Decision:** Start with Python 3.11+ standard library, a command-line entrypoint, validated environment configuration, fixed structured events and automated unittest tests. The code uses no third-party runtime or test dependencies, database, server, background jobs or external services. No dependency lockfile is necessary for an empty third-party dependency set; any future dependency must be reviewed, locked and installed reproducibly. Python itself is a prerequisite, not bundled or pinned by this repository.

**Alternatives:** Node plus a frontend/server could ultimately support a dashboard, but adds an ecosystem and public-server risk before security controls exist. A Python web framework may be useful later but would add a dependency and interface before requirements and security are implemented. A local CLI supplies a verifiable low-cost increment without precluding a web UI.

**Consequences:** The CLI cannot earn money, provide a dashboard or store records. Windows commands are documented but not yet verified on Windows. Production labels do not imply production isolation or readiness. Later phases must reconsider interfaces, authentication, database, dependency management and deployment against their actual needs, with a recorded plan and tests.

## Database and migrations

At the Phase 1 checkpoint no database or schema existed. Phase 2 introduced the auth-only schema described below. Phase 3 has begun a separate state-storage foundation described in ADR-003. Before business records or additional schema upgrades, review location and permissions, versioned migration and rollback strategy, transaction and ownership boundaries, and backup/restore procedures; test restoration against a copy. Never overwrite or delete existing data without authorization and a verified recovery plan.

## ADR-002: Auth-only local SQLite prerequisite (Phase 2)

**Status:** Experimental; Phase 2 offline scope passed with documented limitations, not production-ready. An auth-only versioned SQLite database is a necessary dependency for persistent authentication, throttling, session revocation and audits. It does not implement Phase 3's business state machines, migrations, ledger or task durability. The database path is explicitly supplied; the application creates no database during the `status` command. The schema initializes only an empty file and refuses unknown versions; no data is automatically migrated or deleted.

Python's standard library implements PBKDF2-HMAC-SHA256, cryptographic randomness, SQLite and hidden terminal input without introducing a dependency. POSIX owner/mode checks are enforced. User-reported Windows ACL tests, hidden-input confirmation and an isolated no-runtime-gate-override candidate run now support preparing an experimental Windows development path. Per-operation Windows ACL checks remain required; the user reported the unpatched checkout and missing-database tests passed at `72241cc`. Trusted ancestors and a trusted OS account are assumptions, not enforced OS isolation. A future web interface requires a new reviewed design for session cookies, CSRF, transport, access control, deployment and audit.

## ADR-003: Separate explicitly initialized SQLite state (Phase 3)

**Status:** Experimental infrastructure only, 2026-10-30; Phase 3 remains in progress.

**Decision:** Use standard-library SQLite in a separate absolute, private path chosen by the caller, outside the repository. File creation requires explicit confirmation and exclusive creation; existing, empty and unknown files are never initialized. The distinct application ID and strict version 1 bootstrap identify the database. SQLite transactions provide atomic bootstrap and internal operations; SQLite's online backup copies a checked committed snapshot to a newly created private path. A restore is another checked copy to a new path. Neither operation replaces a file, and failure can leave a partial file to inspect. The auth database is not migrated, backed up or restored by this facility.

**Alternatives and consequences:** Extending the existing auth schema risks mixing recovery and schema boundaries; PostgreSQL or an external provider introduces services and credentials unnecessary for this offline prototype. The separate store is not an authorization boundary. Narrow internal opportunity-draft, task-planning, order-estimate and invoice-draft services are now present, but they do not discover opportunities, execute tasks or create financial commitments. An internal session-bound unit of work holds auth then state locks and derives actor identity from a live session, but does not enforce ownership of records. Its two database commits are not atomic together: a failure after state commits can produce an ambiguous result. Services must use a fixed trusted auth/state pair and durable idempotency before exposure. The supported upgrades are explicit, backup-first v1-to-v2, v2-to-v3, v3-to-v4, v4-to-v5, v5-to-v6, v6-to-v7, v7-to-v8, v8-to-v9, v9-to-v10, v10-to-v11 and v11-to-v12 migrations, each under a write lock and transactional schema change; new files still start at version 1. Other upgrades are refused. The opportunity-draft, task-plan, queue, task-workflow, order-draft, invoice-draft and unverified-payment-placeholder services write fixed audit events and owner-scoped idempotency keys atomically with their records; this is not complete business audit or general-purpose idempotency. Future migrations need their own atomicity, compatibility, backup and recovery tests. Trusted filesystem ancestors and OS account remain assumptions; path-swap races and hostile local access are not eliminated. `quick_check` is not a substitute for a full integrity/security audit or real disaster-recovery testing. The user reported passing Windows tests through the v3 draft service; the user also reported passing v4/v5 task and order tests on Windows; the user later reported passing tests through v9; the v10 association and v11 local completion are unverified there. Real-data recovery and an independent audit remain unverified.


## ADR-004: Explicit local task completion marker (Phase 3)

**Status:** Experimental, 2026-10-30; Phase 3 remains in progress.

**Decision:** Preserve historical queue/workflow schemas and their legitimate independent histories. Add an opt-in v11 marker in an explicit backup-first migration, and recognize a marked queue as logically completed only in version-aware service reads. Atomically approve matching pending output and record the marker, retry result and audit under a live session and current lease. Block new rescheduling of marked items; preserve historical exact retries.

**Alternatives and consequences:** Treating cancellation as completion misstates history; silently treating an unmarked leased queue as complete would confuse consumers. Rebuilding the old queue table/status check would change historical schema and require broader migration and replay design. A marker preserves those schemas but means raw SQL and older consumers see `leased`, and it does not prevent independent parent operations before completion, prove human review, fence external effects or authenticate history. Coordinated deletion of marker/history may be undetectable. These limitations block a Phase 3 exit; a later integrated lifecycle must address them without stranding valid pending or exhausted work. Recovery uses a checked snapshot to a new path, with no automatic replacement or downgrade. No real data was migrated.


## ADR-005: Opt-in managed local pairs (Phase 3)

**Status:** Experimental, 2026-10-31; Phase 3 remains in progress.

**Decision:** Introduce a versioned managed marker only for newly associated fresh parents; reject new independent queue/workflow mutations in application services for marked pairs, retaining exact historical retry acknowledgements. Keep legacy associations and historical schemas unchanged. Require the paired lifecycle for local work and `complete_pair` for local approval, and retain explicit abandonment for recoverable unfinished cases.

**Alternatives and consequences:** Silently making all existing associations managed could strand legitimate divergent, exhausted or pending-review histories. Rebuilding old schemas or enforcing through database triggers would change compatibility and recovery assumptions. Opt-in service-level guards preserve legacy behavior but cannot protect against raw SQLite or coordinated history erasure and do not guarantee external exactly-once work, business correctness, authenticated human review or financial settlement. Managed items are omitted from ordinary ready listings; owner-scoped v12 listing and ready observations can rediscover their association IDs, and atomic local selection can lease a ready managed pair; no worker or external-effect fence is implemented. Migrations are backup-first and never automatic; snapshot rollback omits subsequent operations. Windows v12 and real-data recovery remain unverified.


### Fresh legacy adoption remains explicit

Legacy associations are discoverable through bounded owner-scoped observations. A confirmed v12 adoption is allowed only for a never-leased queued parent and planned workflow; no bulk or automatic conversion, no attempt to reconcile active work, and no new schema version. Adoption writes a managed marker and its operation/audit atomically while preserving the original identity. Nonfresh legacy cases are deliberately left unresolved rather than inventing safety or discarding history. This is local application policy, not an external-effect fence.


### Draft-only financial withdrawal

Coordinate withdrawal of local proposed order/invoice drafts and unverified observations as an explicitly confirmed, atomic, bounded operation rather than silently cascading from existing APIs. Preserve independent legacy behavior and its limitations. Do not invent payment confirmation, settlement or financial revenue, or imply that withdrawing an internal draft affects an external invoice/payment. The later financial and provider models still need independent design and verification.


### Separate unverified financial assertions

Model local potential-to-initiated assertions separately from verified finance. The v13 schema intentionally has no confirmed or settled status and no earnings calculation; every read labels values unverified and not revenue. Forward transitions and withdrawal are local record changes only and require explicit confirmation. Checked migration and prior-service compatibility preserve existing data semantics. This is a partial Phase 3 state model, not a contract/invoice/payment lifecycle or permission to use real financial data.


### Synthetic event policy is not financial verification

Keep the local payment-event policy deliberately disconnected and explicitly synthetic: no credentials, live network, database mutation or real provider. HMAC proves only possession of the supplied test key over the submitted bytes; it cannot prove payment or settlement. The ordered, replay-checked in-memory exercise informs a later provider-specific design but does not satisfy provider sandbox, durable reconciliation, or Phase 3 exit. Real verification requires independently trusted integration and evidence, with its own security and approval gates.


### Durable synthetic events do not become real payments

The previously in-memory test helper lost its entire replay history at restart. V14 adds a separately labelled owner-scoped synthetic journal and checked migration to exercise durable event idempotency and recovery. It deliberately does not integrate with `FinancialAssertions` or promote actual verified/settled state. A caller-chosen synthetic HMAC key cannot establish real provider identity or bank settlement. Future actual financial workflows require a separately selected and independently authenticated provider, durable reconciliation and approval gates; merely passing these synthetic tests does not satisfy them.
