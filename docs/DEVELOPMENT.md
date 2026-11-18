# Development guide

The runtime application has no third-party dependencies. Static-analysis tools are development-only and pinned in `requirements-dev.txt`.

```powershell
python -m pip install -r requirements-dev.txt
python -m ruff check ai_earn/migrations ai_earn/settlement_reconciliation.py ai_earn/ai_provider_config.py ai_earn/postgres_adapter.py ai_earn/connectors/auth.py ai_earn/connectors/drive.py ai_earn/connectors/gmail.py ai_earn/connectors/github.py ai_earn/connectors/client.py tests/test_remaining_roadmap.py ai_earn/financial_math.py tests/test_migration_modules.py tests/test_financial_math.py
python -m mypy
python -m compileall -q ai_earn tests tools
python -m unittest discover -v
```

The Ruff and strict Mypy CI gate currently covers the new migration modules, exact financial arithmetic module, and their contract tests. It is intentionally incremental: the pre-existing application has a large lint/type baseline that has not been cleaned up or suppressed wholesale. Expand the configured scope as adjacent modules are brought to a clean gate; do not mark repository-wide strict typing complete until all intended source files are covered without errors.

Migration maintenance and disposable DB test instructions are in `STATE_MIGRATIONS.md`. Windows developers should additionally run the Windows security and terminal harness tests documented in `WINDOWS_TESTING.md`.
