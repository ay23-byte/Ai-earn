# User Guide — Ai-earn (2026-11-17, branch arena/a2b30614-ai-earn, LATEST 28)

> **Warning:** Experimental, local-only, disposable data, not production. `production_ready:false`. Any “income” you see is **unverified** until a real provider confirms settlement.

## Quick start (POSIX)

```sh
python3 -m venv .venv
.venv/bin/python -m ai_earn status  # {ok:true, production_ready:false}
mkdir -m 700 ~/.ai-earn-private
export AI_EARN_AUTH_DB=~/.ai-earn-private/auth.sqlite3
export AI_EARN_STATE_DB=~/.ai-earn-private/state.sqlite3
.venv/bin/python -m ai_earn auth register   # hidden password, save one-time code
.venv/bin/python -m ai_earn tracker init    # type YES, creates state.sqlite3 + 21 before-v*.sqlite3 (v2→v22)
.venv/bin/python -m ai_earn tracker open` + `data {export|delete}` (owner-scoped export 50MB redacted, deletion `DELETE <8>`) + `health [--json] [--with-backups]` + `production`    # login: 1-4 financial, 5-8 opportunities, 9-12 services/portfolio, 13-33 full menu, 0=logout
```

Windows: same via PowerShell `create_private_directory` (see `README.md`). The private dir must be on a fixed NTFS drive, not a reparse point; never reuse an untrusted ancestor.

## Everyday use

- **Add financial:** `tracker open` + `data {export|delete}` (owner-scoped export 50MB redacted, deletion `DELETE <8>`) + `health [--json] [--with-backups]` + `production`` → `1` → title → INR amount (e.g., `123.45`). Stored as `potential` (unverified), amount immutable.
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
- **Add communication (v18):** `19` → customer ID → channel 1-30 → body 1-2000. Stored as `draft` (unverified). `20` list, `21` sent/archive.
- **Ledger (v18):** `22` → 1 add (kind income/expense/refund/fee, amount INR, note 0-500), 2 list, 3 net (income - expense - refund - fee, unverified). For 11-concept:
- **Invoices (v20):** `28` → order ID → creates `draft` (invoice_number auto `INV-...`, amount snapshot). `29` list (status draft/issued/paid/overdue/void + order filter, pages 50). `30` manage → 1 issued (stores PDF stub SHA-256), 2 paid, 3 overdue, 4 void, 5 pdf (prints stub, checks hash) — all require `YES` except pdf.
- **Ledger 11-concept (v20):** `Ledger.eleven_concepts` / `DashboardMetrics.ledger_11` / `dashboard` `11-CONCEPT` — 11 buckets: proposed, quoted, contracted, invoiced_asserted, invoiced_drafts, invoices_issued (PDF stub), payment_initiated (all unverified), payment_received_verified (`not_implemented` 0), payment_settled (`synthetic`), refunds/expenses/fees (unverified), ledger_net_unverified, net_realized_synthetic = settled(synthetic) - refunds - fees - costs (not bank-verified).
- **Add followup (v19):** `23` → customer ID → due timestamp 0..4102444800 → note 0-500. Stored as `pending` (unverified). `24` list (customer/status filters, pages 50), `25` complete/cancel (1 completed, 2 cancelled, requires YES).
- **CSV (v19):** `26` import (paste lines, end with `.`, prefix 1-64 for idempotency, header name,contact,source, 1000-row/1M limit), `27` export (CSV to terminal).
- **Provider sandbox (v21):** `31` create order (amount INR, generates `order_...` sandbox), `32` list (provider/status filters), `33` webhook (paste JSON + sig, verified HMAC raw-body before parse, amount binding, idempotent event_id).
- **Connectors (v22):** `46` list (gmail/drive/github/manual, stub sandbox), `47` fetch (choose gmail/drive/github, limit 1-100, bounded normalize, throttle 10/min), `48` import via connector (choose connector + prefix 1-64, limit, `import_from_connector` idempotent, manual fallback).
- **Analytics (v22):** `42` summary (funnel+conversion+MRR+workload all-time glance), `43` funnel (by-status in window), `44` MRR monthly (ledger income buckets UTC), `45` export CSV (1M cap, header metric,period_from,period_to,value,verification,note).
- **AI assistant (v22):** `34` list tools (read/draft/consequential), `35` request approval (consequential only, bounded JSON, TTL 15m), `36` list approvals (status filter), `37` decide (approved/rejected, explicit YES), `38` revoke (pending/approved), `39` AI chat (draft_unverified, external content as data, no auto-execution), `40` emergency stop/clear (explicit YES, blocks consequential), `41` execute tool (approval binding, emergency check).
- **List services:** `10` → optional status filter draft/published/archived → pages 50.
- **Manage service:** `11` → ID → 1 publish / 2 archive → YES (transitions `draft→published→archived` etc, `draft→archived` allowed).
- **Portfolio:** `12` → 1 add item (service ID + title + description) / 2 list items (service filter, pages 50). Items are demo only, linked via FK `service_id`.
- **Dashboard:** `python -m ai_earn dashboard snapshot  # or: python -m ai_earn data export (50MB redacted, owner-scoped) / python -m ai_earn data delete (DELETE <8> two-phase)
python -m ai_earn health --json --with-backups  # 27 backups expected, production_ready:false` (needs login, interactive), then `python -m http.server 8000 --bind 127.0.0.1` and open `dashboard/connected.html` → `Load snapshot` → pick `dashboard-snapshot-*.json` beside state DB. `Verified income` is `Not available` (no ledger).

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
- Service catalog & portfolio are experimental local drafts only (v16 `services` draft→published→archived, `portfolio_items` demo, unverified). Product workspace & customers are experimental local drafts only (v17 `products` draft→published→archived, `customers` lead→…→converted/rejected, unverified). Communications & ledger are experimental local drafts only (v18 `communications` draft→sent→archived, `ledger_entries` income/expense/refund/fee net unverified). Followups & CSV are experimental local drafts only (v19 `followups` pending→completed/cancelled, customer CSV import/export 1M/1000-row limits, unverified). No external send/settlement, no delivery proof, no external publish/sync.
- No verified CRM, no invoicing PDF beyond stub, no verified ledger settlement, no Razorpay/Stripe live, no autonomous AI. `followups` pending→completed/cancelled and CSV (v19) are local drafts only, not verified external tasks; `invoices` v20 are local stubs with deterministic PDF `STUB|11-CONCEPT` and SHA-256 hash, not issued demands. Do not enter real customer/financial data.

See `docs/ARCHITECTURE.md`, `DATABASE.md`, `PAYMENTS.md`, `SECURITY_REVIEW.md` for limits.
