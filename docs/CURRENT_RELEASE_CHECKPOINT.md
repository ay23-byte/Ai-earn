# Current Release Checkpoint

**Checkpoint date:** 2026-11-21  
**Canonical code baseline:** `main` at PR #55 merge commit `2ec6b8c3cd5670216ebb7dba0a40d920d7b0288f`  
**Purpose:** provide a current summary without rewriting the historical phase log or treating old branch snapshots as current evidence.

## Current product boundary

Ai-earn is a local-first freelance workflow and business-management application. The repository contains opportunity intake and ranking, service/portfolio workflows, proposals and outreach drafts, customer/CRM and client-linked projects, tasks, invoices and finance records, sandbox payment adapters, AI drafting with approval controls, analytics, local authentication, workspace API/dashboard components, and backup/recovery foundations.

These capabilities are not equivalent to a production SaaS release. Operator acceptance and end-to-end verification on the supported Windows environment remain necessary. The application must not be described as guaranteeing income or autonomously obtaining paying clients.

## Release gates

| Gate | Current evidence-based status | Required next action |
|---|---|---|
| Phase 3 CRM/project workspace | PR #55 merged to main | Complete operator Windows smoke test |
| Post-merge CI | Run [#228](https://github.com/ay23-byte/Ai-earn/actions/runs/44795965244): both Ubuntu jobs succeeded; both Windows jobs were still reported in progress at this checkpoint | Recheck final conclusions; do not record green until all four jobs complete |
| Audit documentation | PR #56 open as draft | Review and merge only after its checks and content are accepted |
| Documentation consistency | Historical README/phase/roadmap snapshots conflict | Reconcile each current-facing document against one audited commit; preserve historical records as history |
| Full business workflow | Individual modules and tests exist; complete operator journey not yet evidenced | Rehearse opportunity → proposal → customer → project → tasks → invoice → payment observation/reconciliation using disposable data |
| Backup and recovery | Code and disposable-database tests/documentation exist | Run a restore drill on the operator's Windows machine; never overwrite the working database |
| Accessibility | Automated checks and partial manual validation documented | Complete the outstanding human browser review |
| Independent security review | Pending | Commission scoped independent penetration testing and remediate findings |
| Hosted SaaS deployment | Not established | Make an explicit hosted-product decision and design TLS, secrets, database lifecycle, tenant isolation, monitoring, incident response, and backup ownership |
| Live payments / verified settlement | Blocked by design | Keep live mode disabled until provider/KYC verification, all required checks, real reconciliation evidence, and accountable human sign-off are complete |

## Payment and data safety

- Sandbox webhook signatures and local reconciliation are not proof of a real payment or bank settlement.
- Never mark income as verified from a client-side success screen, a manually entered claim, or a sandbox event.
- Do not store card data.
- Do not expose the loopback/local application or SQLite files directly to the public internet.
- Use disposable records for release rehearsals until the independent security review and operator acceptance are complete.
- Preserve existing databases and backups; never use a destructive reset as a troubleshooting shortcut.

## Historical documentation policy

The phase register and older audits contain detailed historical snapshots from earlier branches. Those records should remain available as history. A later amendment must identify its audited commit, evidence, test command/results, platform, skips, and limitations. Do not copy a test total from an older branch or infer that a historical phase passed on the current commit.

## Next execution sequence

1. Recheck the four jobs in post-merge CI run #228 and the checks for PR #56.
2. Run the documented Windows Phase 3 and backup/recovery smoke procedures on the operator's actual machine with disposable data.
3. Reconcile the README and phase register using the exact current commit and verified test totals.
4. Add or improve an automated end-to-end regression test for the complete local business workflow, then run the full supported CI matrix.
5. Complete manual accessibility review and independent security testing.
6. Decide whether the target remains local-first or becomes hosted SaaS; implement the corresponding operational design as a separate release track.
7. Evaluate live payment enablement only after all required provider, settlement, security, and human approval gates are satisfied.

## Verification record

At this checkpoint, the GitHub API reported the post-merge run #228 as `in_progress`; Python 3.11 and 3.13 Ubuntu jobs were `success`, while both Windows jobs were `in_progress`. The PR #56 run likewise reported both Ubuntu jobs successful and both Windows jobs in progress. The workflow job-log endpoint did not provide downloadable logs for those in-progress jobs. No final all-platform CI pass is claimed here.

This file is a status record, not a release approval, security certification, deployment authorization, or payment authorization.
