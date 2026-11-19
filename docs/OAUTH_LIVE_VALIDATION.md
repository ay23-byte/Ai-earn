# OAuth Live Validation Procedure (disposable accounts only)

**Status: DRIVE + GMAIL LIVE-VALIDATED (operator run, 2026-11-19).** The
operator ran the full lifecycle below against a disposable Google test
project: Drive returned 0 items (expected under the `drive.file` scope on a
fresh account) and Gmail returned 10 metadata items; both runs revoked
credentials locally. GitHub live validation remains open (optional token).
Never put credentials in the repo or chat — environment only.

## Verified without a live account (in CI)

Authorization URL + PKCE (S256), state validation, redirect-URI binding,
code exchange, in-memory access/refresh tokens, auto-renewal on expiry with
rotation, revocation erasing credentials, owner isolation, throttling/retry,
least-privilege scopes (`drive.file`, `gmail.readonly`, GitHub read token).

## Live lifecycle (operator runs, ~10 minutes)

1. Create a disposable Google Cloud test project + OAuth client; add the
   loopback redirect URI your flow prints.
2. In your shell only:
   `AI_EARN_CONNECTOR_LIVE=1`, `AI_EARN_GOOGLE_CLIENT_ID=...`,
   `AI_EARN_GOOGLE_REDIRECT_URI=...`, `AI_EARN_OWNER_ID=<32-hex>`.
3. `python -m tools.live_provider_check` — must report all OK (dry run,
   contacts nothing).
4. `python -m tools.live_provider_check --live --service drive` — open the
   printed URL, consent with the disposable account; the tool validates the
   callback, exchanges the code, lists ≤10 items (count only), then revokes.
5. Repeat with `--service gmail`. For GitHub set `AI_EARN_GITHUB_TOKEN`
   (disposable, read-only) and use `--service github`.
6. Confirm in the provider console that access shows revoked.

Success = tool prints `Validated <service>: N item(s) returned`. Record the
date/service/result (no tokens) in `docs/IMPLEMENTATION_ROADMAP.md` P2-2.
