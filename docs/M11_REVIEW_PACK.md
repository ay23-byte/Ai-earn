# M11 Live-Payment Review Pack — human sign-off walkthrough

**Status: UNSIGNED. Live payments remain BLOCKED in code.** This pack prepares
the review; it does not perform it. The accountable human must personally
verify each item, record evidence, and sign. An agent cannot sign.

## Automated evidence (re-run at review time)

```
python -m ai_earn.production_readiness  # or evaluate(os.environ)
python -m unittest discover
python -m tools.security_check && python -m tools.security_check --audit
```

Latest agent-run drill (disposable data, 2026-11-18, re-run for v1.0 RC): **11/11 checks PASS** —
security_review, webhook_verification, reconciliation, duplicate_handling,
refunds, failed_payments, settlement_records, audit_trails, backups,
error_recovery, configuration_separation — with `live_blocked: true`,
`live_ready: false`. Passing automated checks is necessary, not sufficient.

## v1.0 RC evidence attached

- `docs/V1_RELEASE_AUDIT.md`, `docs/V1_RELEASE_REPORT.md`,
  `docs/DATABASE_RELEASE_VALIDATION.md`, `docs/SECURITY_RELEASE_REVIEW.md`,
  `docs/ACCESSIBILITY_RELEASE_VALIDATION.md`,
  `docs/BACKUP_RECOVERY_RELEASE_VALIDATION.md`, `docs/CI_RELEASE_VALIDATION.md`.
- OAuth live validation: Drive + Gmail COMPLETED by operator 2026-11-19
  (see `docs/OAUTH_LIVE_VALIDATION.md`). Pentest: PENDING (third party).

## Human checklist (each requires a named reviewer + date)

- [ ] I have read `docs/PRODUCTION_READINESS.md` and `docs/PAYMENTS.md` in full.
- [ ] I understand sandbox/HMAC settlement is not bank settlement, and that
      `SettlementReconciliation` verifies only against operator-supplied CSV
      evidence plus explicit human confirmation.
- [ ] I accept financial, tax, and regulatory responsibility for live charges,
      refunds, disputes, and reconciliation in every jurisdiction served.
- [ ] Provider accounts (Razorpay/Stripe) are in my name/entity, webhook
      secrets are provisioned via environment only, and test-mode end-to-end
      payment + refund + dispute drills passed on disposable accounts.
- [ ] Incident response, key rotation, and rollback owners are named below.
- [ ] I authorize the code change that lifts the live block as a separate,
      reviewed commit referencing this signed pack.

Reviewer (print/sign): ______________________  Date: __________
Incident owner: ______________________  Key-rotation owner: ______________________

## After signing

1. Store this signed pack out-of-repo (never commit signatures or secrets).
2. Open a separate PR that (a) references the pack's storage location,
   (b) lifts `ProviderConfig` live blocking, (c) keeps `live_blocked: true`
   until deployment review completes.
3. Re-run the full CI matrix and the production drill on the exact merge
   commit; record results in `docs/PRODUCTION_READINESS.md`.
