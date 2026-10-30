# Windows Phase 3 verification

This is a **disposable-data test**, not a Phase 3 exit decision, security audit, production approval or real-data recovery. Phase 3 remains in progress. Do not use customer data, real passwords, recovery codes, API keys, or payment credentials.

## Before running

- Obtain a checkout that includes `tools/windows_phase3_gate.py` after these changes are available on GitHub. The existing Windows checkout is `F:\AI-EARN\Ai-earn`; preserve any local commits, untracked files, databases and backups. Do not reset, clean or overwrite them to make the gate pass.
- Use Windows, Git on `PATH`, and Python 3.11+; the existing `.venv` may be reused. The checkout and Windows temporary directory must be on a local fixed drive with persistent ACLs and without reparse-point ancestors. The tests create and clean up their own synthetic temporary data.
- The gate requires Git to report a clean checkout at the repository root. It checks the commit and working-tree status before and after the tests; it does not independently attest ignored files, the Python environment, or changes that are made and reverted during the run.

## Run from PowerShell

After safely obtaining the version containing this gate, open a terminal in the repository root. For the previously reported Windows checkout:

```powershell
Set-Location 'F:\AI-EARN\Ai-earn'
git status --short --branch
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m tools.windows_phase3_gate
$LASTEXITCODE
```

If the checkout is not clean, stop and inspect your own changes; do not delete them. If Git or the virtual environment is unavailable, resolve that separately without overwriting your files. The gate prints the exact commit and Python version, then runs Windows security/authentication and Phase 3 focused tests followed by full discovery. It requires real Windows authentication tests to run rather than skip, requires at least one executed test in every focused module, and rejects unexpected skips in those modules. Four explicitly POSIX-specific tests may be skipped there; other platform-specific skips outside the focused modules may occur during full discovery. Expected failures and unexpected successes are not treated as a pass. Future legitimately platform-specific tests may require a reviewed update to the explicit skip list.

A successful run ends with `WINDOWS PHASE 3 TESTS PASSED` and exit code `0`. Codes `1` and `2` indicate failure or a refused precondition. A successful result is **only test evidence for that checkout**; it does not establish safe recovery, actual work or payment verification, or Phase 3 completion. The gate cannot prove a real-data restore and does not execute live financial actions.

If a test fails, stop rather than bypassing it. Share the commit, Python version, final counts, whether required Windows tests ran, and any relevant test names and errors. Tracebacks or `git status` can reveal local paths or filenames; redact these if desired. Never share credentials, `.env` files, database contents or recovery codes. If a test is interrupted, inspect any reported temporary path before touching it; preserve existing user files.
