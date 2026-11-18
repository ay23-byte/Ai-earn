# Security Release Review

## Verified controls (automated, this release)

- Passwords hashed (no plaintext storage paths); sessions owner-bound with
  expiry; recovery codes single-use; secrets environment-only, never logged
  (connector client filters api_key/token/secret from logs).
- Owner isolation enforced server-side on every resource; cross-owner tests
  pass. SQL access is parameterized; dashboard renders snapshot data without
  innerHTML injection; dangerous paths rejected; production config fails
  closed when required values are missing.
- `tools.security_check` (secret patterns, 0.0.0.0 DB exposure, .env) and
  `--audit` (stdlib-only runtime → no third-party vulns to audit) pass.
- OAuth: PKCE S256, state, least-privilege scopes, in-memory tokens,
  refresh rotation, revoke-erase.

## Known limitations (pre-disclosed)

- A hostile process running as the owning OS account can edit SQLite
  directly; storage is not tamper-evident against same-user attackers.
- Settlement verification is operator CSV evidence + explicit human
  confirmation, not bank authentication. Live payments are blocked in code.

## External assessment

**Independent penetration testing: PENDING.** Automated checks are not a
pentest. The engagement package is `docs/PENTEST_HANDOFF.md` (architecture,
threat model `docs/THREAT_MODEL.md`, auth design, API inventory in
`docs/API.md`, data flows, known limitations, test instructions, checklist).
No certification is claimed.
