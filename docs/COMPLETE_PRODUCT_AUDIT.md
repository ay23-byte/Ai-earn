# Complete Product Audit (verified against code, 2026-11-19)

Base: `main` @ `9c6c742`. Method: module-by-module code inspection + test
inventory, not documentation claims. Prior release evidence (PRs #43–#49)
is re-used where the code is unchanged.

## Priority matrix

| Priority | Area | Current State | Required Work | Verification |
|---|---|---|---|---|
| P0 | Security | Implemented + automated-verified; pentest pending | External pentest only | Security suite, scans; handoff ready |
| P0 | Revenue workflows | Full local cycle implemented (opportunity→proposal→project→invoice→sandbox payment→ledger→profit) | Live money blocked pending M11 | `tests/test_e2e_journey.py`, finance suites |
| P0 | Financial system | Integer paise, idempotency, reconciliation, PDF invoices (stdlib) | None in-repo | Finance/invoice/reconciliation suites |
| P1 | Client acquisition | Opportunities (scoring with published heuristic assumptions), CRM pipeline, CSV import/export, follow-ups | Real opportunity sourcing is the operator's (no ToS-violating scraping, by design) | Opportunity/CRM suites |
| P1 | AI automation | Provider abstraction, approval queue, emergency stop, usage tracking; drafts unverified | Real provider keys = operator config | AI suites (mocked transport) |
| P1 | User experience | Tracker CLI (85+ menu actions) + read-only snapshot dashboard; axe 0 violations (automated + operator manual Settings view) | Remaining manual a11y views = operator | Frontend/SPA tests, axe |
| P2 | Deployment | Local-first verified; Postgres adapter RLS-proven; hosted = documented, not claimed | Hosted infra = operator decision | Deployment docs, CI matrix |

## Genuinely missing found by this audit (and fixed in this PR)

- **Guided first-run onboarding (§16): was missing.** Added
  `ai_earn/onboarding.py` + `python -m ai_earn onboarding`: a read-only
  checklist computed from the owner's actual records (service created/
  published, customer, opportunity, proposal, work planned, invoice,
  income recorded, AI configured) with a concrete next action. No demo
  data is created or mixed with real records. 3 new tests incl.
  owner-isolation and bad-token denial.

## Honestly not implemented / not applicable

- Live payment processing: **blocked in code** pending M11 owner sign-off
  (security review + provider config + operational validation required).
- Public hosted SaaS / public service pages: **not claimed** — the shipped
  architecture is local-first with a read-only snapshot dashboard.
- Automated job applications/outreach: **refused by design** (ToS and
  spam risk); AI prepares drafts, humans approve and send.
- External platform scraping (Upwork/Fiverr/LinkedIn): **not implemented**
  — no official API access; manual import is the supported path.

## What prevents actual revenue generation today

Nothing in the software for the *local* cycle: the owner can source an
opportunity (manually), score it, draft/review a proposal, run the
project, invoice (PDF), record payment evidence, reconcile, and compute
profit. The blockers are external: finding real clients (operator),
provider/AI keys (operator), live payment sign-off (owner, M11),
independent pentest (third party).
