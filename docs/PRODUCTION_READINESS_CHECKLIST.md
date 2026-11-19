# Production Readiness Checklist (v1.0 RC)

- [x] Full test suite green locally (1,037 tests, 18 classified skips)
- [x] Ruff + Mypy (basic + widened strict scope) clean
- [x] Security scan + dependency audit clean; no secrets committed
- [x] Migrations v29 verified on clean + existing disposable DBs
- [x] Multi-user/owner isolation tests pass
- [x] Finance integer-paise consistency + reconciliation verified
- [x] AI consequential actions gated by ApprovalQueue
- [x] Backup/restore drill validated on disposable data
- [x] axe-core: 0 violations (jsdom); manual browser items documented
- [x] Live OAuth validation run — Drive, Gmail, and GitHub validated by operator 2026-11-19
- [ ] Independent pentest — PENDING: third party (handoff ready)
- [ ] Manual a11y browser pass — PENDING: human
- [ ] M11 sign-off — PENDING: accountable human; live payments stay BLOCKED
- [ ] CI green on the release PR (4 legs) — recorded in CI validation doc
