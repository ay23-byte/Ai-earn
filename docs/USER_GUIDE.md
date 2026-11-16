# User Guide — Ai-earn (2026-11-16, branch arena/c6693e06-ai-earn)

> **Warning:** Experimental, local-only, disposable data, not production. `production_ready:false`. Any “income” you see is **unverified** until a real provider confirms settlement.

## Quick start (POSIX)

```sh
python3 -m venv .venv
.venv/bin/python -m ai_earn status  # {ok:true, production_ready:false}
mkdir -m 700 ~/.ai-earn-private
export AI_EARN_AUTH_DB=~/.ai-earn-private/auth.sqlite3
export AI_EARN_STATE_DB=~/.ai-earn-private/state.sqlite3
.venv/bin/python -m ai_earn auth register   # hidden password, save one-time code
.venv/bin/python -m ai_earn tracker init    # type YES, creates state.sqlite3 + 14 before-v*.sqlite3 (v2→v15)
.venv/bin/python -m ai_earn tracker open    # login, 1=add, 2=list, 3=advance, 4=withdraw, 0=logout (opportunities via API, see docs/DATABASE.md)
```

Windows: same via PowerShell `create_private_directory` (see `README.md`). The private dir must be on a fixed NTFS drive, not a reparse point; never reuse an untrusted ancestor.

## Everyday use

- **Add opportunity:** `tracker open` → `1` → title → INR amount (e.g., `123.45`). Stored as `potential` (unverified), amount immutable.
- **Advance:** `3` → ID → `YES` → `potential → quote_asserted → contract_asserted → invoice_asserted → initiated_asserted` (each requires `YES`, `confirmed=True` in service).
- **Withdraw:** `4` → ID → `YES` → `withdrawn`.
- **List:** `2` → pages 100 at a time, `YES` for next.
- **Dashboard:** `python -m ai_earn dashboard snapshot` (needs login, interactive), then `python -m http.server 8000 --bind 127.0.0.1` and open `dashboard/connected.html` → `Load snapshot` → pick `dashboard-snapshot-*.json` beside state DB. `Verified income` is `Not available` (no ledger).

## Troubleshooting with doctor

```sh
python -m ai_earn doctor          # human + JSON
python -m ai_earn doctor --json   # machine only
python -m ai_earn doctor --plan-init  # recovery steps
```

Statuses: `not_configured` (set env), `relative_path`, `invalid_path`, `permission_error` (fix 0700/0600 or Windows ACL), `missing` (restore or `tracker init`), `empty_file`, `not_a_database`, `unsupported_schema`/`incompatible_schema`, `ok`.

## Backups

- **Do not** delete `before-v*.sqlite3`. To backup: `python -c "from pathlib import Path; from ai_earn.state_storage import StateStorage; StateStorage(Path(os.environ['AI_EARN_STATE_DB'])).backup_to(Path('~/backup/state.sqlite3'), confirmed=True)"` to a **new** private path.
- Restore = copy backup **to new path**, verify with `doctor`, swap env.

## Security tips

- Use a personal OS account, not shared. Anyone with DB read/write can bypass local auth.
- Save recovery code **outside** the private dir, paper or password manager.
- Sessions expire in 30 min; `R` after login → `YES` replaces code (old invalidated).
- Rate-limited (too many → wait 15 min). Never paste passwords/codes in chat.
- Keep `AI_EARN_*_DB` on same trusted parent; never in repo.

## What is not ready

- Opportunity discovery is experimental local drafts only (v15 `opportunities`, manual/connector, unverified, draft ranking, stub AI helpers). No verified external source, no evidence scoring. No CRM, no invoicing PDF, no ledger net income, no Razorpay/Stripe live, no autonomous AI. Do not enter real customer/financial data.

See `docs/ARCHITECTURE.md`, `DATABASE.md`, `PAYMENTS.md`, `SECURITY_REVIEW.md` for limits.
