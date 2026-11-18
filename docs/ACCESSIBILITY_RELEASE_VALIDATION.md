# Accessibility Release Validation

## Automated — VERIFIED (2026-11-18)

axe-core (WCAG 2/2.1 A+AA) via `tools/accessibility_audit.mjs` (jsdom):
**0 violations** — index.html (7 passes), app.html (24 passes),
connected.html (18 passes). Static checks in
`tests/test_completion_hardening.py`: form labels, aria-live status regions,
skip link, no innerHTML injection.

## Incomplete under jsdom — MANUAL, still required

color-contrast (needs canvas), bypass, aria-prohibited-attr, and
label-content-name-mismatch could not be fully evaluated headlessly.
The shipped frontend is the read-only dashboard + terminal CLI; there is no
registration/login web UI, so web-form workflow testing is NOT APPLICABLE.

## Manual checklist (human, real browser)

- [ ] Keyboard-only: tab order, visible focus, skip link on all 3 pages.
- [ ] Contrast in both themes at 320px and 1024px (axe in DevTools).
- [ ] Screen reader pass (NVDA/VoiceOver) over dashboard snapshot states:
      loading, empty, error, populated.
- [ ] Record results + date here. No manual certification is claimed yet.
