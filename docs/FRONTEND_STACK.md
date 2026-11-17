# Frontend Stack — Phase 15 Decision

**Date:** 2026-11-17
**Branch:** `arena/a2b30614-ai-earn`

## Existing stack inspected

- `dashboard/app.html` — single HTML file, 379L Phase 14 SPA, 17 views, no build step, loads via `<script src="app.js" defer>`, `<link href="app.css">`.
- `dashboard/app.css` — 58L, 6924 B, pure CSS variables (`--bg`, `--panel`, `--line`, `--cyan`), `[data-theme="light"]` toggle, `color-scheme: dark light`, shell/top/nav/metrics/streams/rows, responsive `@media 1100/820/560`.
- `dashboard/app.js` — vanilla JS, no framework, no `package.json`, no `node_modules`, `npm run dev` not present. Hash router (`location.hash` + `hashchange`), `normalizeRoute`, `applyTheme` (localStorage + `prefers-color-scheme`), `FileReader` snapshot loader (1 MB cap, `read_only` check, `ai-earn-dashboard-snapshot` validation), `render(snapshot)` reads `snapshot.metrics` only, never `fetch`.
- `dashboard/index.html` — meta `color-scheme`, redirects `location.replace("app.html")`, preserves legacy `connected.html` link.
- No bundler, no CDN, no `fetch("http")`, fully offline-first `file://` compatible.
- Tests: `tests/test_frontend_spa.py` asserts 17 `data-route`, 17 `data-view`, viewport/color-scheme, dark/light CSS, responsive breakpoints, skip link, no mock amounts, no network fetch.

## Decision: stay vanilla — no framework added

**Justification**

- **Offline-first constraint wins.** Snapshot is a user-selected JSON file (private `state_storage.py` data). The app must work from `file://` and `python -m http.server` without a build. Adding React/Vue/Svelte would require Node, bundler, and build output, breaking the single-file `load snapshot` flow and the 1 MB size guard.
- **Scope fits vanilla.** 17 views are static panels rendered from `snapshot.metrics` (opportunities/services/proposals/customers/projects/tasks/ai-assistant/digital-products/orders/invoices/payments/expenses/analytics/integrations/settings/security). Reusable helpers (`renderByStatus`, `renderChart`, `setText`, `escapeHtml`, `showToast`, `showLoading`) already give component reuse without a framework.
- **Premium polish achievable in CSS.** Dark/light tokens, `Inter` + `letter-spacing` typography, `--radius`/`--shadow`, `fadeIn`/`slideIn`/`spin` animations, hover `transform: translateY(-1px)`, `bar-track`/`bar-fill` charts, empty dashed states, and responsive grid (4→2→1) all work without a framework.
- **Accessibility & correctness over novelty.** Existing code already handles `skip` link, `aria-current="page"`, `aria-live="polite"`, `tabindex="-1"` main, `role="banner"/"status"`, focus-visible outlines. Keeping vanilla avoids framework a11y pitfalls.
- **Risk & maintenance.** No `npm audit`, no lockfile churn. Tests remain pure Python file-content checks; no DOM harness needed. Future contributors can read `app.js` top-to-bottom in one file.
- **Migration path preserved.** If the app ever needs live sync, a framework can be introduced behind the same hash routes via aliases (`overview→dashboard`, `portfolio→proposals`, etc. already in `ALIASES`). For Phase 15 the cost outweighs the benefit, so we extend vanilla: `hero`/`pill`/`panel-head`/`field`/`field-error`/`toast`/`spinner`/`chart` primitives, `initTheme`/`initNavToggle`/`initRouter`/`initSnapshotLoader`/`initFilters`/`initRevenueHandlers`/`initSettingsForm`.

**Outcome**

Vanilla SPA extended for Phase 15 premium SaaS:
- 17 exact nav items: Dashboard→Security & Audit Logs (assessed vs legacy 17)
- Dark/light persistent theme, consistent spacing, typography hierarchy, subtle animations
- Professional bar charts, useful empty states, responsive (`820`/`560`), accessible, validated settings form, toast notifications, reusable components
- All controls wired to real `snapshot.metrics` — no mocks, no `fetch("http")`, no `localhost`.
