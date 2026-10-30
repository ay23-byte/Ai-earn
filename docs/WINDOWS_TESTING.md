# Windows Phase 2 test instructions

This is a **test-only** procedure. Windows authentication remains disabled in normal use until the results are reviewed; do not change the verification gate or use these tests with real account data.

## Requirements

- A Windows laptop with Python 3.11 or newer. The `py` launcher is optional; an existing virtual environment's Python also works.
- Use the checkout or extract a supplied test bundle to a separate folder on a local fixed drive, not a network share. The temporary directory used by Windows must also be on a local fixed drive supporting persistent ACLs, without reparse-point ancestors. The test creates and deletes disposable test data in its own temporary folder.
- No API keys, customer information, real passwords, recovery codes, or payment credentials are required. Tests use synthetic values only.

## Run

Open PowerShell in the checkout or extracted folder containing `ai_earn` and `tests`. Use Python 3.11+; if your existing virtual environment is in a separate checkout, call that Python by its full path instead of creating or changing files in the checkout. For example, for the previously reported checkout:

```powershell
& 'F:\AI-EARN\Ai-earn\.venv\Scripts\python.exe' --version
& 'F:\AI-EARN\Ai-earn\.venv\Scripts\python.exe' -m unittest tests.test_windows_security tests.test_windows_auth -v
& 'F:\AI-EARN\Ai-earn\.venv\Scripts\python.exe' -m unittest discover -v
```

These commands run code from the current folder. If using a separate extracted bundle, the checkout's virtual environment supplies only the interpreter; do not overlay the bundle onto an existing checkout or database.

Check that the `tests.test_windows_auth.WindowsAuthenticationTests` tests **run rather than being skipped**. Tests exercise creation of a protected test directory, inherited database/journal ACLs, unsafe directory/file ACL rejection, junction rejection, account isolation, revocation, recovery, rate limiting and a disposable backup. They temporarily use the Windows `icacls.exe` and `cmd.exe` tools only on their own temporary test paths. The standard Windows account workflow is not enabled by the tests. The CLI tests mock hidden input, including the new code-replacement tests. The user reported both replacement tests passed in a 15-test Windows targeted run with Python 3.13.5 after pulling `main` to `4858bfb`; the full 70-test run then passed with 33 POSIX-specific skips. A subsequent session-time fix and new Windows regression test have not yet been tested on Windows. The prior disposable terminal result is described below; normal, unpatched Windows authentication still needs verification before Phase 2 can pass.

If any test fails, stop; do not edit the application to bypass it. Share the test names, error messages, and final summary. Tracebacks can contain your Windows user name or local paths—remove those if you prefer. Do not share any real password, recovery code, `.env` file, or database. A passing test is useful evidence but not a security audit; the agent must review the results before updating the Phase 2 gate.

## Actual Windows terminal check

The user reported that all 13 earlier Windows-specific tests passed and the full 51-test run passed with 21 POSIX-only skips. They also reported completing the separate disposable terminal harness, including registration, login, recovery and another login; its output reported `TEST COMPLETE` and temporary-directory removal. The transcript showed blank input prompts, but the user has not separately confirmed hidden input or supplied Python version/commit identity for that earlier terminal run. The harness uses a process-local test override and **does not enable normal Windows auth**. It predates and does not test the replacement feature. If rerunning, use only an interactive terminal and the synthetic password displayed by the program. Use the disposable recovery code only locally; do not share it. Report just the final success/failure summary and whether password and recovery-code input was hidden.

If the utility is available as `tools/windows_terminal_smoke.py` in the checkout, run:

```powershell
.\.venv\Scripts\python.exe -m tools.windows_terminal_smoke
```

If supplied as a standalone download, save it as `windows_terminal_smoke.py` in the repository root (the folder containing `ai_earn`) and run:

```powershell
.\.venv\Scripts\python.exe .\windows_terminal_smoke.py
```

It runs registration, login, recovery and a second login, using a synthetic username and the stated test-only password. Save the displayed **test-only** recovery code locally for the recovery step; it and the account are discarded when the temporary directory is removed. It fails closed if the private directory cannot be created or the terminal is unavailable. Do not bypass errors or use real account credentials.

## Recovery

The tests use `tempfile.TemporaryDirectory` and should remove their own temporary files. Do not remove or modify any existing user database. If a test is interrupted, inspect the temporary path reported by Windows before manually deleting anything. No app database or user account should be created for this procedure.
