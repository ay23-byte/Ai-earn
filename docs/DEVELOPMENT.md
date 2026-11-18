# Development guide

The runtime application has no third-party dependencies. Static-analysis tools are development-only and pinned in `requirements-dev.txt`.

```powershell
python -m pip install -r requirements-dev.txt
python -m ruff check ai_earn tests tools
python -m mypy
python -m mypy --strict ai_earn/financial_math.py ai_earn/migrations
python -m compileall -q ai_earn tests tools
python -m unittest discover -v
```

The Ruff and strict Mypy CI gate currently covers the new migration modules, exact financial arithmetic module, and their contract tests. It is intentionally incremental: the pre-existing application has a large lint/type baseline that has not been cleaned up or suppressed wholesale. Expand the configured scope as adjacent modules are brought to a clean gate; do not mark repository-wide strict typing complete until all intended source files are covered without errors.

Migration maintenance and disposable DB test instructions are in `STATE_MIGRATIONS.md`. Windows developers should additionally run the Windows security and terminal harness tests documented in `WINDOWS_TESTING.md`.
