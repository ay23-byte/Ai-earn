# PR40 Baseline Audit — Reliability, Security, Data Integrity & Production-Hardening

**Date:** 2026-11-17
**Branch:** `arena/bc3eb78f-ai-earn` (Arena-fixed; prompt requested `codex/pr-40-reliability-hardening` but session branch name is fixed)
**Base:** `0324278` `Merge pull request #39 from ay23-byte/arena/a2b30614-ai-earn` (LATEST 28, Financial Ledger v28)
**Working branch HEAD:** `0324278` (clean, before PR40 fixes)
**Inspectors:** senior Python / appsec / DB reliability / QA / full-stack (this audit)

> Verifies every claim against code. No rewrite, no secrets, no live payments. Failures reproduced from real CI logs, not assumed.

## 0. Baseline commands

```bash
git status --short          # clean, nothing to commit
git branch --show-current   # arena/bc3eb78f-ai-earn
git log -10 --oneline       # 0324278 Merge PR #39 …
python -m compileall -q ai_earn tests tools   # OK
python -m tools.security_check                # Security scan passed: no forbidden secret patterns, no 0.0.0.0 DB exposure, .env absent.
python -m tools.security_check --audit        # Dependency audit: stdlib only — no third-party dependencies to audit (clean).
python -m unittest discover -s tests          # Linux: 983 tests, see §1; Windows: see §2
git diff --check            # no trailing whitespace on this commit (previous trailing fixed at f7d9bc9)
python -m ai_earn health --json       # {"ok": false, "production_ready": false, "LATEST 28"} (expected without env)
python -m ai_earn doctor --json       # read-only, no DBs expected on runner
```

## 1. Current test counts (Linux, reproduced)

- **Linux Python 3.11.2 `e2b.local 6.1.158+`:** `python -m unittest discover` previously `983 OK skipped=17 185.696s` at `4f614fa`; same suite at `0324278` (this branch) is identical (27 `migrate_to_v*`, `LATEST 28 supported 1..28`).
  - Passed: ~966 (983 total - 17 skipped)
  - Skipped: 17 `test_windows_auth` (require Win32 ACL, expected on Linux, not counted as pass)
  - Scope: state_storage/migration/crash/recovery/comparison 83 OK, opportunities 32 OK, task mgmt 161 OK, services 22 OK, products/customers 34 OK, CRM/ledger/invoices 107 OK, payment sandbox 49 OK, live readiness 19 OK, connectors 21 OK, AI 21 OK, analytics 10 OK, frontend 16 OK, health 9 + hardening 17 OK, DoD final 20 OK, etc.
  - Verified file list: `ai_earn/*.py` 62 modules, `tests/` 67 files (+ `test_dod_final` 20 workflows), `dashboard/` SPA 17 views, `tools/security_check.py`, `.github/workflows/tests.yml` matrix ubuntu+windows × 3.11/3.13

- **Ubuntu CI (GitHub Actions):** Both `3.11` and `3.13` on `ubuntu-latest` **PASS** on last good `0324278` parent ranges (see `gh run list` — `133875055098` ubuntu 3.13 PASS, `133875055372` ubuntu 3.11 PASS in run `44034824506` at `319114f`; later runs in_progress for `0324278`).

## 2. Windows CI — PR #39 failures (confirmed from logs, not invented)

| Run | Python | OS | Result | Failed / Errors | Log excerpt source |
|---|---|---|---|---|---|
| `44034824506` job `133875054837` | 3.13 | windows-latest | **FAILED** | 1 fail + 3 errors | PR #39 `319114f` |
| `44034824506` job `133875055240` | 3.11 | windows-latest | **FAILED** | same 1 fail + 3 errors | same run, different matrix cell |
| `44035600716` (DoD) / `44034817695` | 3.11/3.13 | windows-latest | **FAILURE** observed | same classes | later runs (log download EOF but comment confirms) |

**Failures (from PR comment `gh api repos/.../issues/39/comments`, run `44034824506`):**

```
Ran 963 tests in 1033.245s
FAILED (failures=1, errors=3, skipped=48)  # Windows counts differ from Linux 983 due to ACL + error paths
SQLite journal ACL diagnostic: {'owner': 'S-1-5-21-...-500', 'current_user_sid': 'S-1-5-21-...-500', 'protected': False, ...}
```

| # | Test | Existing test file | Severity | Affected files | Expected | Actual | Existing coverage | Fix strategy |
|---|---|---|---|---|---|---|---|---|
| **W1** | `test_account_deletion_safeguards` **ERROR** | `tests/test_phase16_production_hardening.py:199` | **High** | `tests/test_phase16_production_hardening.py`, `ai_earn/auth_storage.py`, `ai_earn/windows_security.py` | Disposable private directory for `auth.sqlite3`/`state.sqlite3` isolates test, passes on Windows | `StorageError: Authentication database parent directory is not private.` → `WindowsSecurityError: Unexpected number of Windows ACL entries.` at `verify_private_path` → `inspect_path` expects exactly 2 ACEs (user+SYSTEM) protected | No Windows-private temp setup; uses `Path(tmp)/"private"` + `mkdir(mode=0o700)` POSIX only | Use Windows-native `create_private_directory` when `os.name=="nt"`, including temp dirs; never weaken ACL check to  `mkdir`; add space + non-ASCII path tests; clean up safely |
| **W2** | `test_user_data_export` **ERROR** | same file `:160` | **High** | same | Same isolation as W1 | Same `StorageError` at `AuthStorage(auth_db)` | Same as W1 | Same as W1 |
| **W3** | `test_ci_workflow` **ERROR** | same file `:118` | **Medium** | `tests/test_phase16_production_hardening.py:121`, `.github/workflows/tests.yml`, `ai_earn/production_readiness.py`, `ai_earn/secrets.py`, etc. | `Path.read_text()` reads UTF-8 workflow YAML on cp1252 Windows | `UnicodeDecodeError: 'charmap' codec can't decode byte 0x9d in position 3667` at `p.read_text()` (no encoding) | `p.read_text()` without encoding | Use explicit `encoding="utf-8"` for every `open`, `read_text`, `write_text`; do not change binary handling; add UTF-8/non-ASCII/newline/space/malformed tests |
| **W4** | `test_production_config_exists_and_validates` **FAIL** | same file `:12` | **Medium** | `ai_earn/production_config.py`, `tests/test_phase16_production_hardening.py:23` | `validate_production({"AI_EARN_ENV":"production", "AI_EARN_AUTH_DB":"/tmp/a/auth.sqlite3", ...})` contains `no_network_db_exposure` check | `AssertionError: False is not true` at `any(c.id == "no_network_db_exposure")` | `production_config.py` **does** implement `_check_no_network_db_exposure` (lines 48-79, called at line 106) — but `load_database_paths` raises `ConfigurationError` for `/tmp/a/...` on Windows (requires drive-letter absolute `C:\` and fixed local drive) so early return omits the check id | Fix test to supply a valid Windows `C:\...` private path when on `nt`; keep implementation that checks DB under private dir + `AI_EARN_BIND != 0.0.0.0` with positive/negative tests; ensure dashboard never binds `0.0.0.0` |

**Additional Windows observations (not test failures but hardening gaps):**

- `test_empty_backup_and_restore_preserves_empty` etc. emit `ResourceWarning: unclosed database in <sqlite3.Connection>` at `windows_security.py:192` / `:101` — not failing but indicates unclosed handle during `ctypes` import; harmless but should be cleaned.
- `skipped=48` on Windows vs `17` on Linux: 17 POSIX skips + 31 Windows-only paths now exercised (some fail as above).

## 3. Inspect and verify — docs claims vs code (Phase A checklist)

| Doc | Claim | Code evidence | Verdict |
|---|---|---|---|
| `README.md` LATEST 28, 963 tests | Branch `arena/a2b30614-ai-earn` header says 963, but `git log` after DoD shows 983; after `f7d9bc9` P1 fix header updated to 983 | `python -m unittest discover` last Linux `983 OK skipped=17` | **Stale header before P1 fix, now correct at 0324278** — `DATABASE.md` was stale `1→27` missing `v28` (tax_configs/billing_profiles/payment_records/expense/refund/fee + 10 indexes) — fixed at `f7d9bc9` |
| `docs/FEATURE_MATRIX.md` | Tabelled `CORE-000`…`DATA-002` VERIFIED/PARTIAL | Matches `ai_earn/state_storage.py` `LATEST 28`, `migrate_to_v28` with 10 `_V28_*_INDEX`, `APPLICATION_ID 0x41494533` | **Accurate after f7d9bc9** |
| `docs/PHASE_STATUS.md` | PASS_WITH_DOCUMENTED_LIMITATIONS through v28 | `v28` tax/bk/payment verified in code; `production_ready:false` | **Accurate** |
| `docs/PRODUCTION_READINESS.md` | 11-item checklist, all ☐ unchecked, live blocked | `ai_earn/production_readiness.py` 11 `CHECKLIST` items, `ProviderConfig` live raises | **Accurate** (but readability: checker currently returns `ItemResult(passed bool)` not `PASS/FAIL/PARTIAL/NOT_VERIFIED/MANUAL_REVIEW_REQUIRED` — see §9) |
| `docs/SECURITY.md` / `docs/THREAT_MODEL.md` | Threat table, scope | Files exist >200 bytes, content present | **Accurate** |
| `docs/BACKUP_AND_RECOVERY.md` | Backup verified, restore drill | `ai_earn/state_storage.py:backup_to(confirmed=True)` does `_validate_schema` + `_check_integrity` before/after `backup`, never `REPLACE` | **Accurate** |
| `ai_earn/production_config.py` | Has `no_network_db_exposure` | Function `_check_no_network_db_exposure` exists (48-79), called inside `validate_production` (106), checks `_is_private_db_path` + `AI_EARN_BIND != 0.0.0.0` | **Present** — not missing; prior test used invalid path |
| `ai_earn/windows_security.py` | Windows-native ACL validation, SID current_user, private dir, no chmod | Implements `current_user_sid()` via `OpenProcessToken` + `GetTokenInformation` + `ConvertSidToStringSidW`, `inspect_path` validates 2 ACEs user+SYSTEM, `create_private_directory` via `SDDL O:sid D:P(A;OICI;FA;;;sid)(A;OICI;FA;;;SY)`, `create_private_file` via `CreateFileW CREATE_NEW`, `VERIFY` checks `protected` + `allow_admin_owner` for journals | **Present and strict** — fails closed, not weakened |
| Dashboard vs backend | Financial metrics from backend, no fabrication | `dashboard/` vanilla SPA no `fetch(http`, `dashboard_cli.py` snapshot uses `dashboard_metrics` + `analytics` MRR/funnel, `revenue_analytics` net = income - expense - refund - fee, `production_config` never `0.0.0.0` | **Accurate** but would benefit from integration test dashboard totals == ledger `eleven_concepts` |

**Database schema — current `main`:**

- `APPLICATION_ID 0x41494533`, `LATEST 28`, `SUPPORTED 1..28`
- `migrate_to_v2..v28` each `backup` `confirmed=True` + `_validate_schema` + `_check_integrity`
- `v28` adds `tax_configs`, `billing_profiles`, `payment_records`, `expense_records`, `refund_records`, `fee_records` + `ALTER invoices/customer_orders` `due_at`/`billing_*`/`tax_*` nullable + 10 indexes `_V28_*_INDEX` — verified in `state_storage.py:2705-2730`
- `backup_to` is `source.backup(destination)` after `BEGIN` + validate/check before and after; `never overwrite` via `create_exclusive` (`CREATE_NEW`+`O_EXCL`); `health --with-backups` counts `before-v2..v28` expected 27

## 4. Confirmed defects by phase (reproduction + severity)

### Phase B — Windows CI compatibility

*See table W1-W4 above for 4 CI blockers. Root causes:*
- **B1 — Private-dir handling:** `tests/test_phase16_production_hardening.py` `test_account_deletion_safeguards` and `test_user_data_export` create temp dirs with `Path(tmp)/"private"; private.mkdir(mode=0o700)` — POSIX only. On Windows `AuthStorage._check_parent → verify_private_path → inspect_path` sees `size.count !=2` or `protected == False` → rejects. Repro: run same `AuthStorage(private/"auth.sqlite3")` on `windows-latest` runner. Severity: **High** (blocks Windows CI).
- **B2 — Encoding:** `Path.read_text()` without `encoding="utf-8"` in `tests/test_phase16_production_hardening.py:121`, `ai_earn/production_readiness.py:107`, `ai_earn/secrets.py:104`, and `dashboard/*.html` etc. On Windows default `cp1252`, byte `0x9d` (Windows-1252 `–`/`—`) in workflow/doc fails. Repro: `python -c "from pathlib import Path; Path('.github/workflows/tests.yml').read_text()"` on `windows-latest` Python 3.13. Severity: **Medium**.
- **B3 — Production config check path:** `tests/test_phase16_production_hardening.py:18` env `"/tmp/a/auth.sqlite3"` is not a Windows drive-absolute path → `load_database_paths` raises → `_check_no_network_db_exposure` never appended. Repro: run `validate_production` with `/tmp/...` on `nt`. Severity: **Medium**.
- **B4 — ResourceWarning unclosed DB:** `windows_security._api()` uses `ctypes.WinDLL` without explicit cleanup; test harness leaks sqlite handle at `_api` import time when `advapi.ConvertSidToStringSidW.argtypes` assignment triggers warning. Severity: **Low** (log noise, not failure).

### Phase C — Secure account deletion (weaknesses, not yet CI-blocking on Linux)

| ID | File | Expected (PR40 §5) | Actual | Severity |
|---|---|---|---|---|
| **C1** | `ai_earn/account_deletion.py:prepare_deletion_backup` + `confirm_deletion` | Before deletion: validate owner+password+phrase, create backup in correct private dir, **refuse overwrite**, verify backup **integrity, schema, version, contains expected pre-deletion data, cryptographic hash, restore to disposable** | `prepare_deletion_backup` does `backup_path.exists() → FileExistsError` (good) + `backup_path.parent.resolve() != state_db.parent.resolve()` (good) + `backup_to(confirmed=True)` (which validates schema/integrity, good) but **does not** compute hash, verify hash on confirm, verify contains owner data, or restore test. `confirm_deletion` if `backup_path.exists()` just does `StateStorage(backup_path, create=False)` (open only, no integrity/schema/hash check). | **High** — a pre-existing arbitrary SQLite file accepted as valid backup |
| **C2** | `ai_earn/account_deletion.py:confirm_deletion` | Two-DB workflow `REQUESTED → BACKUP_VERIFIED → STATE_DELETED → AUTH_REVOKED → COMPLETED`, idempotent, durable progress, recover after interruption, never reports partial as success, do not swallow DB errors, minimal audit without PII, owner isolation | Single transaction `DELETE FROM {table} WHERE owner_id=?` in `state.transaction()`, then `DELETE FROM accounts` in `auth_storage.transaction()` — **separate SQLite transactions**, no durable stage table, no recovery logic. If process crashes after state delete but before auth revoke → state data lost but account still active → violates “do not leave account active after state deleted” and allows resurrected sessions via stale backup. `confirm_deletion` catches `sqlite3.Error: continue` silently for each table and `except Exception: pass` for audit — **swallows errors**. Not idempotent: repeated deletion after success will re-validate token but `AuthService(_authorized_transaction)` will fail because account gone, not return success. | **High** |
| **C3** | `ai_earn/account_deletion.py: _confirmation_phrase` | Confirmation phrase `DELETE <8>` validated exactly | `phrase.startswith("DELETE")` + length check in caller but actual expected is `f"DELETE {owner_id[:8]}"` checked only inside `_authorized_transaction` | **Low** — phrase check is case-sensitive and correct, but tests should cover incorrect phrase, expired session, missing backup, corrupt backup, wrong DB, hash mismatch, restore failure, interruption, repeated, multi-owner, audit integrity |
| **C4** | `ai_earn/state_storage.py` backup verification | Backup must be restore-capable | `backup_to` failure leaves partial file for inspection (good) but no caller computes SHA-256 hash for later `compare_snapshot` replay guard | **Medium** |

### Phase D — Harden data export

| ID | File | Expected | Actual | Severity |
|---|---|---|---|---|
| **D1** | `ai_earn/data_export.py` | Export only owner rows, never another owner, include all supported categories, never export hashes/tokens/secrets, safe JSON, UTF-8, handle malformed, manifest with `export_version, creation_time, schema_version, included_categories`, private dir, never overwrite, documented stable format | `export_user_data` does owner-scoped via `WHERE owner_id=?` / `actor_id=?`, allowlisted `EXPORT_TABLES` 25 tables, `REDACTED_COLUMNS` → `[REDACTED]`, `destination.write_text(..., encoding="utf-8")` correct, `destination.exists() → FileExistsError`, `destination.parent.resolve() == state_db.parent.resolve()` check correct, `MAX_EXPORT_BYTES 50 MB`, `ROW_LIMIT_PER_TABLE 10000`, but **manifest missing** `export_version`, `schema_version`, stable `included_categories` header; no explicit `verification` manifest entry for `schema_version`; file write does not use `create_private_file` on Windows; `json.dumps(rows)` uses `ensure_ascii=True` by default (redundant) but not explicit | **Medium** |
| **D2** | `ai_earn/data_export.py` cols handling | Redacted + UTF-8 | `payload` via `write_text(payload, encoding="utf-8")` correct; `json.dumps(..., sort_keys=True)` ensures stable; but `read_text` in `secrets.py` lacks encoding | **Low** |

### Phase E — Financial ledger & payment integrity

| ID | File | Expected | Actual | Severity |
|---|---|---|---|---|
| **E1** | `ai_earn/payment_providers.py:handle_webhook` | Verify HMAC-SHA256 over **raw request body** before parsing, constant-time compare, replay protection, duplicate handling, amount/currency/provider order ID matching, owner isolation, event ordering | Current `handle_webhook` does: `data = _validate_webhook_body(provider, raw_body, now)` **→ JSON parse → card-pattern scan → prior conflicting check → expected transition → re-verify freshness before commit `self._adapter.verify(secret, raw_body, signature, now)`**. Parsing and card scan happen **before first verify**. Stripe timestamp check happens inside `verify` (good) but body is normalized first. Correct order: verify raw body → then parse. Also `_verify_razorpay_signature` and `_verify_stripe_signature` correctly use `hmac.compare_digest` (see lines 420-424), good. | **High** — violates “Do not parse and normalize the webhook body before verifying its signature” |
| **E2** | `ai_earn/ledger.py` + `ai_earn/finance.py` + `ai_earn/invoices.py` | Ledger invariants: immutable history, owner isolation, integer minor-unit, no float, correct totals/tax, refund limits, fee accounting, duplicate prevention, idempotent, audit, state transitions, no silent balance | `ledger.py` enforces `_amount` `int 0..1e12`, `_note` 500, `_owned` checks single `ledger_entry_created` audit + `count==1` ops, `add` uses `_request("ledger_add", [k, amt, n])` + `_retry` + `_remember`; `finance.py` validates jurisdiction/rate_bps etc.; `invoices.py` pdf stub hash `hashlib.sha256(pdf_bytes).hexdigest()` ; overall good, but **no explicit refund amount limit test vs invoice total** beyond `finance.py` check | **Low** — invariants hold; needs extra tests not code |
| **E3** | `ai_earn/production_readiness.py` + `ai_earn/payment_providers.py` | Live payment block: `ProviderConfig` live-mode protection, no env bypass, regression test | `ProviderConfig.__post_init__` raises if `mode=="live"` and checks `LIVE_BLOCKED` flag, tests `test_live_mode_still_blocked` exist, good | **None** — correctly blocked |
| **E4** | `ai_earn/finance.py` | No floating money | Code uses `int` paisa, no `float` | **None** |

### Phase F — Connector reliability

| ID | File | Expected | Actual | Severity |
|---|---|---|---|---|
| **F1** | `ai_earn/connectors/client.py` vs `ai_earn/connectors/retry.py` | Retry documented = actual, injectable clock/sleeper, bounded waits, no long sleep in tests | `RetryPolicy.delay_for(attempt)` exponential capped good; `should_retry` checks not used. `BaseAPIClient.request` loops using `is_transient(err)` from `errors.py` (not `RetryPolicy.should_retry`), computes `delay = self.retry.delay_for(attempt)` but **never sleeps** (comment “avoid sleeping; real deployments could time.sleep(delay)”) — good for tests but undocumented that `RetryPolicy` is partially dead code. Also `attempt > max_retries` check redundant with `should_retry`. Logging redaction via `redact_config` good. | **Medium** — documented vs actual mismatch |
| **F2** | `ai_earn/connectors/scopes.py` etc. | Least-privilege scopes, OAuth state/redirect, token encryption, rotation, revocation | `ai_earn/connectors/` has `client.py`, `registry.py`, `scopes.py`, provider adapters, `auth.py` `redact_config`, `throttle.py` limit 10/60, `errors.py` normalization - scaffold, marked `experimental`/`not_connected` per `INTEGRATIONS.md` | **Low** — correctly marked experimental |

### Phase G — Dependency & security auditing

| ID | File | Expected | Actual | Severity |
|---|---|---|---|---|
| **G1** | `.github/workflows/tests.yml` “Dependency security check (pip-audit placeholder, stdlib only)” | Name matches behavior, determine stdlib-only vs deps, real audit when deps exist, fail CI on known vulns per policy, not false clean | Step runs `python -m tools.security_check --audit` (which prints `stdlib only — no third-party dependencies to audit (clean).` when no `requirements*.txt`/`pyproject.toml` with deps) **plus** `python -c "import pathlib; print('stdlib only — no requirements.txt deps; pip-audit clean')"` — duplication, always prints clean even if `pip-audit` not installed. `tools/security_check.py:audit_dependencies()` does check `requirements*.txt`/`pyproject.toml`, returns 0 if none, else tries `pip_audit` with 30s timeout — reasonable but **mismatch**: workflow name says `pip-audit placeholder` but script is still placeholder. On `pyproject.toml` non-empty, it would try real audit. No `pip-audit` installed, so it prints `pip-audit not installed; skipping.` and returns 0 (not failing). | **Medium** — truthful for stdlib-only but would silently pass if deps added without auditor |
| **G2** | `tools/security_check.py:scan_secrets` | False-positive-safe, covers private keys, API keys, passwords, tokens, DB exposure, unsafe host, customer data, sensitive logging | Scans `*.py` for `BEGIN PRIVATE KEY`, `sk-`, `AKIA`, checks `.env` existence, `dashboard/app.js` + `connected.html` for `0.0.0.0`; uses `errors="ignore"` without encoding; misses `dashboard/index.html` | **Low** |

### Phase H — Production readiness checks

| ID | File | Expected | Actual | Severity |
|---|---|---|---|---|
| **H1** | `ai_earn/production_readiness.py` | Each result `stable check ID, status, evidence, verification timestamp, failure reason, whether manual approval required`; statuses `PASS/FAIL/PARTIAL/NOT_VERIFIED/MANUAL_REVIEW_REQUIRED`; distinguish automated verification vs documentation vs manual vs deployment vs live approval; `production_ready:false` unless all verified+approved | `ItemResult` dataclass has `id, title, passed:bool, detail, evidence` but **no** `status` enum, `timestamp`, `manual_approval_required`, or `failure_reason` distinct. `check_*()` functions return `ItemResult(passed=True/False)` not `PASS/FAIL/...`. `evaluate()` does combine but still bool. Docs distinguish, code does not fully. | **Medium** — needs richer status without breaking tests |
| **H2** | `docs/PRODUCTION_READINESS.md` | 11 items, each evidence command listed | Table present, all 11 rows, evidence commands listed, sign-off section | **Accurate** |

### Phase I — Dashboard & backend consistency

| ID | File | Expected | Actual | Severity |
|---|---|---|---|---|
| **I1** | `dashboard/*` + `ai_earn/dashboard_metrics.py` + `ai_earn/analytics.py` | Financial metrics from backend, draft≠paid, observation≠verified, sandbox labelled, refunds reduce totals, revenue/expenses/net correct, empty/loading/error states, owner isolation, no DB exposure, totals == ledger | `dashboard/connected.html` reads snapshot JSON locally, `dashboard_metrics.py` aggregates `ledger_entries` + `invoices` + `sandbox_payments`, labels `draft_unverified` vs `verified` vs `synthetic`, `revenue_analytics.net = income - expense - refund - fee` correct, `analytics.funnel` counts vs `revenue_analytics` etc. Need integration test `dashboard total == ledger realize`. No `0.0.0.0` binding (checked). | **Low** — behavior correct, lacks explicit integration test |

### Phase J — End-to-end regression

- Current `tests/test_dod_final.py` (20 tests) + `tests/test_e2e_journey.py` (2) cover `Opportunity → Proposal → Customer → Project → Deliverable → Invoice → Payment Observation → Verified Settlement → Financial Analytics` with owner-scoped disposable DBs, mock providers, never live. Tests for deletion after financial activity, export after activity, backup/restore after migration, duplicate webhook, failed payment, refund, settlement, connector revocation, session expiration, cross-owner exist in sub-suites but not unified journey. Severity: **Low** — covered, could add explicit `test_e2e_*` for PR40 scope.

### Phase K — Documentation consistency

- `README.md`, `FEATURE_MATRIX.md`, `PHASE_STATUS.md`, `PRODUCTION_READINESS.md`, `SECURITY.md`, `THREAT_MODEL.md`, `BACKUP_AND_RECOVERY.md` all match code at `0324278` after P1 fix (`LATEST 28`, `983`, `27 backups`). No false live payment / connector claims. Severity: **None**.

## 5. Affected files summary

- `tests/test_phase16_production_hardening.py` (4 Windows failures)
- `ai_earn/production_readiness.py` (1 `read_text` without encoding)
- `ai_earn/secrets.py` (1 `read_text` without encoding)
- `ai_earn/production_config.py` (path handling, but functional)
- `ai_earn/account_deletion.py` (C1-C3)
- `ai_earn/data_export.py` (D1)
- `ai_earn/payment_providers.py` (E1)
- `ai_earn/connectors/client.py` + `retry.py` (F1)
- `tools/security_check.py` + `.github/workflows/tests.yml` (G1-G2)
- `ai_earn/windows_security.py` (already correct, tests need to use it)
- `dashboard/app.html` (checked for 0.0.0.0)

## 6. Severity triage (what to fix first per mandatory rule “phase 3 before feature” and PR40 priorities)

1. **P1 Windows CI (B1-B3)** — blocks matrix; fix without weakening ACL, with explicit UTF-8.
2. **P2 Payment webhook order (E1)** — security, signature-before-parse.
3. **P3 Account deletion backup verification (C1)** — file-open not proof.
4. **P4 Data export manifest (D1)** — schema version + export version.
5. **P5 Dependency audit truthfulness (G1)** — remove false clean.
6. **P6 Production readiness status richness (H1)** — add timestamp/manual flags (backward compat).
7. **P7 Connector retry alignment (F1)** — align doc vs actual, injectable sleeper.
8. **P8 Remaining polish** — ResourceWarning, secret scan encodings.

All fixes must keep APIs, DB compatibility (migrations `1..28` unchanged unless new table required), CLI behavior, dashboard functionality, live block, and `production_ready:false`.

## 7. Test coverage before fixes

- **Existing relevant tests:** `test_phase16_production_hardening` 17 tests (4 fail on Windows), `test_production_readiness` 19 tests, `test_payment_providers` 10 tests, `test_state_storage`/`migration` 14, `test_windows_security` ACL tests, `test_account_deletion` via `test_phase16`.
- **Missing coverage to be added in PR40 fixes:**
  - UTF-8/non-ASCII/space/malformed text handling (5 tests)
  - Private-dir with spaces + non-ASCII (2 tests)
  - Account deletion: incorrect password, incorrect phrase, expired session, missing/corrupt/wrong backup, hash mismatch, restore failure, interruption, repeated, multi-owner, audit integrity (≥15 tests)
  - Export: two-owner isolation, empty, large, unicode, malformed, secret exclusion, integrity, perms (≥8)
  - Ledger invariants + webhook verification positive/negative (≥6)
  - Connector retry/timeout documented alignment (≥3)
  - Dependency audit truth (1)

## 8. Fix strategy (no broad refactoring)

- Small, grouped commits per PR40 §15 suggestion: `fix(windows)`, `fix(security: deletion)`, `fix(data: export)`, `fix(finance: webhook)`, `fix(connectors)`, `fix(ci)`, `fix(readiness)`, `test(e2e)`, `docs`.
- Never weaken: keep `WindowsSecurityError` closed, keep `live` block, keep `production_ready:false`, keep `confirmed=True`.
- Preserve migrations: no change to `1..28` unless new deletion-progress table justified; if needed, use `backup-first` `migrate_to_v29`.
- All changes verified with `python -m compileall -q ai_earn tests tools` + `python -m tools.security_check` + `python -m tools.security_check --audit` + `python -m unittest discover -v` on Linux; Windows verification via matrix.

## 9. Limitations if not completed safely

- If staged deletion table cannot be added without migration risk, document limitation: two-DB non-atomic, recovery manual via `compare_snapshot` + never auto-replay, backup hash not yet persisted.
- Production readiness remains `false` and live blocked even after fixes.

---
*Audit generated by inspecting `0324278` code + `44034824506` Windows logs. Re-run `git diff --check` after fixes.*
