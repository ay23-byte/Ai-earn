# Windows Phase 2 test instructions

This is a **test-only** procedure. Windows authentication remains disabled in normal use until the results are reviewed; do not change the verification gate or use these tests with real account data.

## Requirements

- A Windows laptop with Python 3.11 or newer. The `py` launcher is optional; an existing virtual environment's Python also works.
- Extract the supplied test bundle to a local fixed drive, not a network share. The temporary directory used by Windows must also be on a local fixed drive supporting persistent ACLs, without reparse-point ancestors. The test creates and deletes disposable test data in its own temporary folder.
- No API keys, customer information, real passwords, recovery codes, or payment credentials are required. Tests use synthetic values only.

## Run

Open PowerShell in the extracted folder (the folder containing `ai_earn` and `tests`) and run:

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m unittest tests.test_windows_security tests.test_windows_auth -v
.\.venv\Scripts\python.exe -m unittest discover -v
```

Check that the `tests.test_windows_auth.WindowsAuthenticationTests` tests **run rather than being skipped**. Tests exercise creation of a protected test directory, inherited database/journal ACLs, unsafe directory/file ACL rejection, junction rejection, account isolation, revocation, recovery, rate limiting and a disposable backup. They temporarily use the Windows `icacls.exe` and `cmd.exe` tools only on their own temporary test paths. The standard Windows account workflow is not enabled by the tests. The CLI test mocks hidden input; an actual Windows terminal/getpass workflow still needs separate verification before Phase 2 can pass.

If any test fails, stop; do not edit the application to bypass it. Share the test names, error messages, and final summary. Tracebacks can contain your Windows user name or local paths—remove those if you prefer. Do not share any real password, recovery code, `.env` file, or database. A passing test is useful evidence but not a security audit; the agent must review the results before updating the Phase 2 gate.

## Actual Windows terminal check

The user reported that all 13 Windows-specific tests passed and the full 51-test run passed with 21 POSIX-only skips. The automated CLI test still mocks hidden input. The separate `tools/windows_terminal_smoke.py` checks actual terminal input against an isolated temporary database using a process-local test override; it **does not enable normal Windows auth**. Run only on Windows in an interactive terminal, and use exclusively the synthetic password displayed by the program. It creates a disposable test account and prints a disposable test recovery code. Use that code only locally and do not share it. Report just the final success/failure summary and whether password and recovery-code input was hidden.

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
