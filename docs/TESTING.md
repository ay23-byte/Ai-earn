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

Latest Linux regression (2026-10-30): `python3 -m unittest discover -v` ran **84 tests: 70 passed, 14 Windows-only skipped**; `python3 -m compileall -q ai_earn tests tools` and `git diff --check` passed. Recovery replacement is covered by service and CLI tests, transaction rollback, output/interrupted-flush retry, and a real POSIX terminal test with hidden input. New session-time tests cover expiry, issuance and rate limiting after simulated transaction delay and a real SQLite lock-contention test using a controlled clock. One Windows-specific session-time test was skipped on Linux; the user later reported it passed on Windows. The latest unpatched Windows-development and unsafe-ACL tests have not yet run on Windows. These checks are not an independent security audit.

Use `python3 -m unittest discover -v` and `python3 -m compileall -q ai_earn tests`. Auth tests use temporary, disposable databases and test-only passwords. Coverage includes account registration/login, salted hashes, token digests and expiry, logout, one-time recovery and revocation, rate limits (including concurrent attempts), unauthorized/cross-account access, storage permissions and symlink denial, unknown schemas, SQLite backup to a separate copy, hidden input, and a POSIX pseudoterminal registration/login/recovery workflow. Tests do not prove the system secure, validate a multi-user server, test Windows ACLs, or rehearse real-user disaster recovery. Unsupported non-POSIX/non-Windows account storage is disabled, and POSIX-specific tests are skipped on other platforms. The latest experimental Windows development path needs verification on Windows. Exact results are recorded in the phase register.

### Isolated Windows candidate (user-reported result)

`tools/windows_candidate_smoke.py` requires Windows and a gate already enabled in the copy being tested. It never patches the gate at runtime; it uses mocked CLI input and a new temporary private directory with synthetic data. The separately supplied test-only candidate changes only the gate in the packaged application code; it is not approved for real data or release. Three local utility tests and a POSIX rehearsal of the extracted candidate passed; OS detection and directory creation were substituted and this is not Windows evidence. The user subsequently reported the isolated Windows candidate completed successfully. The current working branch prepares the ordinary Windows development path; its updated unpatched checkout still requires Windows verification and review.

### User-reported Windows result (2026-10-30)

The user shared Windows console output for `.\.venv\Scripts\python.exe -m unittest tests.test_windows_security tests.test_windows_auth -v` (13 passed) and `.\.venv\Scripts\python.exe -m unittest discover -v` (51 total, 30 passed, 21 POSIX-only skips). They subsequently reported a successful disposable Windows terminal run with all four actions and temporary-directory cleanup. These are user-reported results, not runs performed in this sandbox; Python version and exact commit were not supplied. Input prompts appeared blank, and the user later confirmed both password and recovery-code input were hidden. At that checkpoint the terminal harness and Windows integration tests overrode the gate; the current branch prepares a normal development path that still needs Windows verification. See [WINDOWS_TESTING.md](WINDOWS_TESTING.md).

### Latest Windows targeted regression (2026-10-30)

The user reported a `main` fast-forward to `4858bfb`, Python **3.13.5**, and a successful `python -m unittest tests.test_windows_security tests.test_windows_auth -v` run: **15 passed, none skipped**. This includes the two new recovery replacement tests and a test that forced the gate to false; despite its name, that test did not exercise the unpatched gate. The auth integration tests use a process-local gate override and the CLI replacement test mocks hidden input. The user then reported `python -m unittest discover -v`: **70 tests run, 37 passed and 33 POSIX-specific tests skipped**, in 7.985 seconds. These are user-reported results, not an independent security audit or verification of the skipped tests.

### Windows session-time regression (2026-10-30)

The user reported `python -m unittest discover -v`: **75 tests, 38 passed and 37 POSIX-only skips**, in 12.109 seconds. The new Windows session-time test passed. The earlier test named `test_unpatched_windows_auth_remains_disabled` actually mocked the gate to false, so it did not verify the actual default gate; that test has since been corrected and an unpatched CLI rejection test added, neither yet run on Windows. The remainder of the Windows auth integration suite used a process-local override. These results are user-reported, not an independent audit; no exact commit or bundle identity was supplied with this run.

### Terminal harness follow-up (2026-10-30)

`python3 -m unittest discover -v` exited 0: 56 tests total, 47 passed and 9 Windows-only tests skipped. Five new harness unit tests use isolated fakes; they do not verify a real Windows terminal. `python3 -m compileall -q ai_earn tests tools` and `git diff --check` exited 0. `python3 -m tools.windows_terminal_smoke` correctly refused Linux with exit 2. At that checkpoint the manual Windows harness had not yet been run; the later user-reported result is described above and in [WINDOWS_TESTING.md](WINDOWS_TESTING.md).

### POSIX terminal-harness integration (2026-10-30)

`python3 -m unittest tests.test_windows_terminal_harness_pty -v` passed, followed by five successful repetitions. It exercises the disposable harness using real POSIX hidden input and authentication, replacing only Windows detection and directory creation. This checks harness reliability, not Windows behavior. The subsequent `python3 -m unittest discover -v` exited 0: 57 tests total, 48 passed and 9 Windows-only tests skipped. `python3 -m compileall -q ai_earn tests tools` and `git diff --check` exited 0. This checkpoint did not verify Windows; the later user-reported Windows terminal run is described above. Normal Windows authentication remains unverified.

### Windows follow-up implementation

`tests/test_windows_security.py` tests platform-independent ACL-policy decisions. `tests/test_windows_auth.py` is a separate real-Windows suite for Win32 ACL creation and inspection, unsafe ACLs, reparse points, auth and backup; the current tests use the normal development path without a gate override, but have not yet run on Windows. On Linux the real-Windows tests are skipped and must not be counted as verification. At that earlier checkpoint, `python3 -m unittest discover -v` exited 0: 51 tests total, 42 passed and 9 Windows-only tests skipped. `python3 -m compileall -q ai_earn tests` exited 0. Run the Windows commands in [WINDOWS_TESTING.md](WINDOWS_TESTING.md) on an actual Windows host and report the observed results before reviewing the phase gate.
