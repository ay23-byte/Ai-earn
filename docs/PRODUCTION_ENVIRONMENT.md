# Production Environment Reference (no real secrets)

| Variable | Purpose | Required |
|---|---|---|
| `AI_EARN_ENV` | `production` enables fail-closed config checks | for deployment |
| `AI_EARN_STATE_DB` / `AI_EARN_AUTH_DB` | SQLite paths (private dir, 0600/ACL) | defaults provided |
| `AI_EARN_BIND` | local bind for any operator-run helper | keep loopback |
| `AI_EARN_LOG_LEVEL` | structured log level | optional |
| `AI_EARN_CONNECTOR_LIVE=1` | opt-in live connectors | only with disposable/test accounts until M11 |
| `AI_EARN_GOOGLE_CLIENT_ID`, `AI_EARN_GOOGLE_REDIRECT_URI` | OAuth client (env only) | connector use |
| `AI_EARN_GITHUB_TOKEN` | read-only connector token (env only) | connector use |
| `AI_EARN_POSTGRES_DSN`, `AI_EARN_POSTGRES_APP_DSN` | optional Postgres (needs `requirements-postgres.txt`) | Postgres deployments only |
| Payment provider keys/webhook secrets | env only; see `docs/PAYMENTS.md` | **blocked until M11 sign-off** |

Development defaults must not be used in production: production config
validation fails closed on missing required values (verified by
`production_config` tests + the 11/11 readiness drill).
