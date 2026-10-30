# Baseline testing report

Audit date: 2026-10-30. Working tree initially clean at `16bd10f091398cb191d455e1349164c731bbf713`.

| Check | Command or method | Observed result | Meaning |
|---|---|---|---|
| Inventory | `git ls-files -s`; `find . -type f ! -path './.git/*'` | Only `README.md` before audit | No project test/build entrypoints |
| History | `git log --all --stat --oneline` | One initial commit with README | Local reachable history only |
| Generic test discovery | `python3 -m unittest discover -v` | Exit 0; **Ran 0 tests** | Not an application test pass |
| Manifest and configuration inventory | Python `pathlib` check for common manifests, lockfiles, Docker, test config and `.env.example` | All checked paths absent | No project install, lint, typecheck, dependency audit or build command available |
| Limited secret pattern scan | Python `re` scan of tracked file content in every locally reachable commit | One commit scanned; no matches for selected key/token/credential-assignment patterns | Narrow heuristic, not a complete scan or guarantee |
| Tool availability | Version checks and `command -v` | Python 3.11.2, Node 22.22.3, npm 10.9.8; Docker and gitleaks/trufflehog/detect-secrets unavailable | Environment availability only |
| Audit document structure | One-off Python `pathlib`/`re` validation of the six documents | 17 phases and 33 unique requirement IDs found; relative links and trailing whitespace passed | Documentation structure only, not product functionality or security |
| Tracked changes whitespace | `git diff --check` | Exit 0, no output | Does not inspect untracked audit documents; those were checked separately |
| Untracked diff check attempt | `git diff --no-index --check /dev/null docs/ARCHITECTURE.md` (inside a `set -e` loop) | Exit 1, no output; the loop stopped on the first file | No-index reports a difference from `/dev/null`; this attempt is not counted as a pass. Python checked all audit files separately. |

No app exists to start or exercise end to end. No authentication, authorization, migrations, financial behavior, integrations, Windows installation, security posture, performance, or recovery has been tested. A dependency audit is not applicable until dependencies exist; no claim of zero vulnerabilities is made. No external services, credentials or live payments were used.

## Subsequent verification

As code is introduced, replace absence with exact reproducible install, test, lint, typecheck, build and restore commands and record observed results. Include negative authorization and cross-account tests before treating protected features as usable. Record failures and limitations rather than treating a zero-test run as a pass.

## Phase 1 foundation

The Phase 0 results above are historical and describe the initial commit, not the current foundation. Current verification results and exact commands are recorded in the Phase 1 section of [PHASE_STATUS.md](PHASE_STATUS.md). The automated suite uses Python's standard `unittest` and runs with `python -m unittest discover -v`; it covers configuration defaults and rejection, structured log allowlisting/filtering, omission of supplied secrets, error envelopes, failure paths and a clean-copy CLI invocation. It does not test authentication, authorization, a database, a network server, payments or external services because none exist. Passing these tests is not a security audit.

`python -m compileall -q ai_earn tests` checks Python syntax but is not a production build, static type check or lint. No project lint, static type checker or third-party dependency audit is currently configured. Windows execution and external integrations remain unverified.

## Phase 2 interim checks

Use `python3 -m unittest discover -v` and `python3 -m compileall -q ai_earn tests`. Auth tests use temporary, disposable databases and test-only passwords. Coverage includes account registration/login, salted hashes, token digests and expiry, logout, one-time recovery and revocation, rate limits (including concurrent attempts), unauthorized/cross-account access, storage permissions and symlink denial, unknown schemas, SQLite backup to a separate copy, hidden input, and a POSIX pseudoterminal registration/login/recovery workflow. Tests do not prove the system secure, validate a multi-user server, test Windows ACLs, or rehearse real-user disaster recovery. Non-POSIX account storage is intentionally disabled, and POSIX-specific tests are skipped on other platforms. Exact results are recorded in the phase register.

### Windows follow-up implementation

`tests/test_windows_security.py` tests platform-independent ACL-policy decisions. `tests/test_windows_auth.py` is a separate real-Windows suite for Win32 ACL creation and inspection, unsafe ACLs, reparse points, auth and backup; it uses a test-only monkeypatch and does not enable Windows account storage for users. On Linux the real-Windows tests are skipped and must not be counted as verification. The latest `python3 -m unittest discover -v` exited 0: 51 tests total, 42 passed and 9 Windows-only tests skipped. `python3 -m compileall -q ai_earn tests` exited 0. Run the Windows commands in [WINDOWS_TESTING.md](WINDOWS_TESTING.md) on an actual Windows host and report the observed results before reviewing the phase gate.
