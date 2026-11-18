# CI Release Validation

Workflow `.github/workflows/tests.yml`: matrix Ubuntu/Windows × Python
3.11/3.13 (4 legs, all required). Gates per leg: pinned dev tools,
`ruff check ai_earn tests tools`, `mypy`, widened
`mypy --strict` (financial_math, migrations, postgres_adapter,
structured_logging, connectors), compileall, secret/DB-exposure scan,
stdlib dependency audit, health/doctor probes, full `unittest discover`.

## Local equivalents run for this RC (2026-11-18)

All of the above run green locally: 1,037 tests OK. Skipped-test
classification: 17 Windows-only ACL tests (environment-dependent,
intentional — they run on the Windows CI legs), 1 Postgres integration
test (requires external infrastructure; opt-in via DSNs, verified locally
against a real disposable server). No test was skipped to hide a failure.

## CI result for this release

Recorded on the release PR after push (all 4 legs must be green before any
merge; merge requires the owner's explicit instruction).
