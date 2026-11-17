# Secret Management — Ai-earn (Phase 16)

**Rule:** Secrets are **process-environment only**, never committed, never logged, never auto-loaded from `.env`.

## What counts as a secret

- `AI_EARN_AUTH_DB` / `AI_EARN_STATE_DB` **values** (absolute paths) — treated as sensitive (do not paste in tickets).
- Passwords (12–256, entered via `getpass` hidden input, never as CLI arg).
- Recovery codes (43-char urlsafe, displayed once, stored as `recovery_hash = SHA256(code)`).
- Session tokens (43-char, stored as `token_hash`, held in process memory, 30-min expiry).
- `ProviderConfig.webhook_secret` (32–128 bytes, per `provider`+`mode`).
- Any `.env` content.

## How secrets are handled

| Concern | Implementation | Evidence |
|---|---|---|
| **Storage** | `AuthStorage` PBKDF2-HMAC-SHA256 600k + 16B salt, `hmac.compare_digest`, dummy derivation for missing user | `ai_earn/auth.py` + `auth_storage.py` |
| **Redaction** | `secrets.redact()` hashes to `[REDACTED:sha256:8]`; `structured_logging._redact_value` and `error_reporting.report_error` never log raw values | `ai_earn/secrets.py`, `structured_logging.py`, `error_reporting.py` |
| **Validation** | `secrets.validate_secret` checks 32..128 length, rejects whitespace/low-entropy, names only in errors | `secrets.py` |
| **Env-only** | `.env.example` is reference; app never does `load_dotenv`. `production_config` fails if `.env` exists. | `config.py`, `production_config.py` |
| **No logging** | `ConfigurationError` leaks only variable name; `write_event` allowlist; `doctor --json` never prints secrets | `config.py`, `events.py` |
| **CI scan** | `grep -R "BEGIN PRIVATE KEY" / "sk-..."`, `git diff --check`, `secrets.assert_no_secrets_in_repo` | `.github/workflows/tests.yml`, `tools/security_check.py` |
| **Gitignore** | `*.sqlite3*`, `*.db*`, `dashboard-snapshot-*.json`, `.env`, `.env.*` | `.gitignore` |

## Developer duties

1. **Never commit** a secret. If you did, rotate it immediately (password via login→`R`→`YES`; webhook secret via new ProviderConfig).
2. **Set via OS env** (POSIX `export`, Windows `$env:`), not `.env`. Example:
   ```sh
   export AI_EARN_AUTH_DB="$HOME/.ai-earn-private/auth.sqlite3"
   export AI_EARN_STATE_DB="$HOME/.ai-earn-private/state.sqlite3"
   export PAYMENT_WEBHOOK_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
   ```
   On Windows PowerShell:
   ```powershell
   $env:AI_EARN_AUTH_DB = Join-Path $HOME ".ai-earn-private\auth.sqlite3"
   $env:AI_EARN_STATE_DB = Join-Path $HOME ".ai-earn-private\state.sqlite3"
   ```
3. **Rotate** via `python -m ai_earn auth recover` (consumes old code, issues new) or `R`→`YES` while logged in. Store new code **outside** private dir (paper/password manager). Never screenshot.
4. **Scan before push:** `python -m tools.security_check` or `grep -R "BEGIN PRIVATE KEY" ai_earn/`.

## Production guidance

- Use a secret manager (e.g., OS keychain, Vault) to inject env vars at process start; do not commit `.env`.
- Separate `webhook_secret` per `provider`/`mode` (sandbox vs live never share). Validate length 32..128 before use.
- Production `AI_EARN_LOG_LEVEL` must not be `DEBUG`.

See also `docs/PRODUCTION_READINESS.md` (configuration separation) and `docs/DEPLOYMENT.md` (Windows local first, never 0.0.0.0).
