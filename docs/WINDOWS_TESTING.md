# Windows Phase 2 test instructions

This is a **test-only** procedure. Windows authentication remains disabled in normal use until the results are reviewed; do not change the verification gate or use these tests with real account data.

## Requirements

- A Windows laptop with Python 3.11 or newer and the `py` launcher.
- Extract the supplied test bundle to a local fixed drive, not a network share. The temporary directory used by Windows must also be on a local fixed drive supporting persistent ACLs, without reparse-point ancestors. The test creates and deletes disposable test data in its own temporary folder.
- No API keys, customer information, real passwords, recovery codes, or payment credentials are required. Tests use synthetic values only.

## Run

Open PowerShell in the extracted folder (the folder containing `ai_earn` and `tests`) and run:

```powershell
py -3 --version
py -3 -m unittest tests.test_windows_security tests.test_windows_auth -v
py -3 -m unittest discover -v
```

Check that the `tests.test_windows_auth.WindowsAuthenticationTests` tests **run rather than being skipped**. Tests exercise creation of a protected test directory, inherited database/journal ACLs, unsafe directory/file ACL rejection, junction rejection, account isolation, revocation, recovery, rate limiting and a disposable backup. They temporarily use the Windows `icacls.exe` and `cmd.exe` tools only on their own temporary test paths. The standard Windows account workflow is not enabled by the tests. The CLI test mocks hidden input; an actual Windows terminal/getpass workflow still needs separate verification before Phase 2 can pass.

If any test fails, stop; do not edit the application to bypass it. Share the test names, error messages, and final summary. Tracebacks can contain your Windows user name or local paths—remove those if you prefer. Do not share any real password, recovery code, `.env` file, or database. A passing test is useful evidence but not a security audit; the agent must review the results before updating the Phase 2 gate.

## Recovery

The tests use `tempfile.TemporaryDirectory` and should remove their own temporary files. Do not remove or modify any existing user database. If a test is interrupted, inspect the temporary path reported by Windows before manually deleting anything. No app database or user account should be created for this procedure.
