# Production Deployment (shipped architecture: local-first, no HTTP server)

Ai-earn v1.0 ships a local-first Python application + read-only dashboard
snapshot. There is no bundled web server, so HTTPS/cookies/CORS are
NOT APPLICABLE to the shipped artifact; do not expose SQLite or the
dashboard directory on 0.0.0.0.

## Deploy steps (operator)

1. Python 3.11+; clone at the release commit; `pip install -r requirements-dev.txt` only for gates (runtime is stdlib-only).
2. Create a private data directory (POSIX 0700 / Windows ACL helper
   `ai_earn.windows_security.create_private_directory`).
3. Set env per `docs/PRODUCTION_ENVIRONMENT.md`; run migrations (checked
   backup is taken first automatically).
4. Run `python -m unittest discover`, `tools.security_check`, and the
   production-readiness drill; all must pass before use.
5. Schedule checked backups per `docs/BACKUP_RECOVERY_RELEASE_VALIDATION.md`.
6. Optional Postgres: `pip install -r requirements-postgres.txt` + DSNs.

Graceful shutdown = process exit; SQLite WAL + checked backups cover
interruption. Remaining operator actions: hosting, OS patching, backup
retention, and (blocked) M11-gated live payments.
