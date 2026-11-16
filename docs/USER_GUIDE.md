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
.venv/bin/python -m ai_earn tracker init    # type YES, creates state.sqlite3 + 16 before-v*.sqlite3 (v2→v17)
.venv/bin/python -m ai_earn tracker open    # login: 1-4 financial, 5-8 opportunities, 9-12 services/portfolio, 0=logout
```

Windows: same via PowerShell `create_private_directory` (see `README.md`). The private dir must be on a fixed NTFS drive, not a reparse point; never reuse an untrusted ancestor.

## Everyday use

- **Add financial:** `tracker open` → `1` → title → INR amount (e.g., `123.45`). Stored as `potential` (unverified), amount immutable.
- **Advance financial:** `3` → ID → `YES` → `potential → quote_asserted → contract_asserted → invoice_asserted → initiated_asserted` (each requires `YES`, `confirmed=True`).
- **Withdraw financial:** `4` → ID → `YES` → `withdrawn`.
- **List financial:** `2` → pages 100 at a time, `YES` for next.
- **Add opportunity (v15):** `5` → title → source URL (http/https) → budget INR → deadline 0 or future timestamp → risk low/medium/high/unknown → skill tags. Stored as `discovered` (unverified, `budget_not_revenue`).
- **List opportunities:** `6` → optional status/risk filters → pages 50.
- **Manage opportunity:** `7` → ID → 1 vetted / 2 pursuing / 3 archived / 4 dismissed → YES (transitions `discovered→vetted→pursuing→archived/dismissed` etc).
- **Ranked opportunities:** `8` → limit (default 20) → draft heuristic (budget desc, risk low→high, deadline asc), unverified.
- **Add service (v16):** `9` → title → scope → deliverable → price INR → revisions 0-100. Stored as `draft` (unverified catalog, not verified offering).
- **Add product (v17):** `13` → title → description → version `[A-Za-z0-9._-]` → price INR. Stored as `draft` (unverified).
- **Add customer (v17):** `16` → name → contact → source. Stored as `lead` (unverified). Then `15` manage product publish/archive, `18` manage customer contacted/converted/rejected. `14`/`17` list with filters.
- **List services:** `10` → optional status filter draft/published/archived → pages 50.
- **Manage service:** `11` → ID → 1 publish / 2 archive → YES (transitions `draft→published→archived` etc, `draft→archived` allowed).
- **Portfolio:** `12` → 1 add item (service ID + title + description) / 2 list items (service filter, pages 50). Items are demo only, linked via FK `service_id`.
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

- Opportunity discovery is experimental local drafts only (v15 `opportunities`, manual/connector, unverified, draft ranking, stub AI helpers). No verified external source, no evidence scoring.
- Service catalog & portfolio are experimental local drafts only (v16 `services` draft→published→archived, `portfolio_items` demo, unverified). Product workspace & customers are experimental local drafts only (v17 `products` draft→published→archived, `customers` lead→…→converted/rejected, unverified). No customer contract, no delivery proof, no external publish/sync.
- No CRM, no invoicing PDF, no ledger net income, no Razorpay/Stripe live, no autonomous AI. Do not enter real customer/financial data.

See `docs/ARCHITECTURE.md`, `DATABASE.md`, `PAYMENTS.md`, `SECURITY_REVIEW.md` for limits.
