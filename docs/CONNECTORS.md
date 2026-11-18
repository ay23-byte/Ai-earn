# Connector OAuth and live-mode safeguards

## Scope and status

OAuth authorization-code with PKCE, the Google Drive API path, the Gmail API
read path, the GitHub issue-search path, and a loopback-only local callback
listener are implemented in `ai_earn.connectors.auth.OAuthFlow`,
`DriveConnector`, `GmailConnector`, and `GitHubConnector`. Live HTTP is
opt-in only: with `AI_EARN_CONNECTOR_LIVE` unset or anything other than `1`,
connectors read deterministic stub data and perform no provider request.
Live mode fails closed without an authorized, owner-bound session.

This is not a provider certification: no real Google/GitHub account has been
used in the test suite (all transport is mocked). Refresh-token renewal is
not implemented; access tokens remain in process memory only. Use only a
disposable test account and data.

## Google setup (Drive/Gmail)

1. Create a Google OAuth client for a testing project, enable the Drive
   and/or Gmail APIs, add only a disposable test account as a test user.
2. Set `AI_EARN_GOOGLE_CLIENT_ID` and `AI_EARN_GOOGLE_REDIRECT_URI`
   (optional `AI_EARN_GOOGLE_CLIENT_SECRET`). Never commit these values.
3. Set `AI_EARN_CONNECTOR_LIVE=1`, build
   `DriveConnector.from_environment(owner_id=...)` or
   `GmailConnector.from_environment(owner_id=...)`, call
   `begin_authorization(owner_id=...)`, open the URL, then either call
   `wait_for_authorization_callback()` (loopback redirect URIs only) or pass
   the securely received `code`/`state` to
   `exchange_authorization_code(...)` with the same owner.
4. Drive `list` is bounded at 10,000 (pages of ≤1,000). Gmail `search`
   (limit 1–100) uses `format=metadata` with only the Subject header plus
   snippet — bodies are never requested, nothing is modified.
5. `revoke(owner_id=...)` erases the local token even if remote revocation
   cannot be confirmed (a sanitized error is raised in that case).

## GitHub setup

Set `AI_EARN_CONNECTOR_LIVE=1` and `AI_EARN_GITHUB_TOKEN` (a fine-grained,
read-only token), build `GitHubConnector.from_environment(owner_id=...)`,
and call `search(query=..., limit=..., owner_id=...)`. Only
`https://api.github.com/search/issues` is allowlisted. With the flag off,
stub data is returned.

The HTTP layer enforces fixed provider hosts, rejects redirects, disables
environment proxies, caps response bodies, validates OAuth state once, uses
S256 PKCE, allowlists scopes, and sanitizes provider errors.
