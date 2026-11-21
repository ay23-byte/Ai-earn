"""Tests for M14+M15 Frontend SPA (vanilla hash router, 17 sections, real snapshot data, a11y, theme)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_HTML = ROOT / "dashboard" / "app.html"
APP_JS = ROOT / "dashboard" / "app.js"
APP_CSS = ROOT / "dashboard" / "app.css"
INDEX_HTML = ROOT / "dashboard" / "index.html"

# Phase 15 premium navigation — 17 SaaS sections (replaces M14 legacy routes via aliases)
EXPECTED_ROUTES = [
    "dashboard", "opportunities", "services", "proposals",
    "customers", "projects", "tasks", "ai-assistant",
    "digital-products", "orders", "invoices", "payments",
    "expenses", "analytics", "integrations", "settings", "security",
]
# Legacy aliases still routed (overview→dashboard, portfolio→proposals, etc.) for backwards compat
LEGACY_ALIASES = {
    "overview": "dashboard", "portfolio": "proposals", "products": "digital-products",
    "communications": "customers", "followups": "tasks", "ledger": "expenses",
    "provider": "payments", "ai-approvals": "ai-assistant", "connectors": "integrations",
    "queue": "tasks", "workflows": "projects", "diagnostics": "security",
}

class FrontendSPAStructureTests(unittest.TestCase):
    def test_files_exist(self):
        for p in (APP_HTML, APP_JS, APP_CSS, INDEX_HTML):
            self.assertTrue(p.exists(), f"missing {p.name}")

    def test_html_has_viewport_and_color_scheme(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertIn('name="viewport"', html)
        self.assertIn('name="color-scheme"', html)
        self.assertIn('content="dark light"', html)

    def test_html_has_17_nav_links_with_hash_routes(self):
        html = APP_HTML.read_text(encoding="utf-8")
        for route in EXPECTED_ROUTES:
            self.assertIn(f'data-route="{route}"', html, f"missing nav for {route}")
            self.assertIn(f'href="#/{route}"', html, f"missing href for {route}")
        # exactly 17 data-route attributes in nav
        matches = re.findall(r'data-route="([^"]+)"', html)
        # filter to expected routes only (nav has 17)
        nav_routes = [m for m in matches if m in EXPECTED_ROUTES]
        self.assertEqual(len(nav_routes), 17, f"expected 17 nav routes, got {nav_routes}")

    def test_html_has_17_views(self):
        html = APP_HTML.read_text(encoding="utf-8")
        for route in EXPECTED_ROUTES:
            self.assertIn(f'data-view="{route}"', html, f"missing view for {route}")
            self.assertIn(f'id="view-{route}"', html)

    def test_html_has_theme_toggle_and_snapshot_loader(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertIn('id="themeToggle"', html)
        self.assertIn('id="snapshotFile"', html)
        self.assertIn('id="clearSnapshot"', html)
        self.assertIn('id="snapshotStatus"', html)
        self.assertIn('role="status"', html)
        self.assertIn('aria-live="polite"', html)

    def test_html_has_skip_link_and_nav_aria(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertIn('class="skip"', html)
        self.assertIn('href="#main"', html)
        self.assertIn('aria-label="Primary"', html)
        self.assertIn('aria-current="page"', html)

    def test_html_references_app_js_and_css(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertIn('href="app.css"', html)
        self.assertIn('src="app.js"', html)

    def test_no_hardcoded_mock_amounts(self):
        # SPA must not contain fabricated INR values like 12345 hardcoded as demo data
        html = APP_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        # Allow rupees formatter but not literal INR 5000 demo values
        # Check that html does not contain rupee amounts except via placeholder —
        for needle in ["₹5,000", "₹10,000", "500000"]:
            self.assertNotIn(needle, html.lower())
        self.assertNotIn("mockdata", js.lower())
        self.assertNotIn("mockData", js)
        # js must treat snapshot as source, not hardcode amounts
        self.assertIn("ai-earn-dashboard-snapshot", js)

    def test_js_has_router_and_theme(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("hashchange", js)
        self.assertIn("normalizeRoute", js)
        self.assertIn("applyTheme", js)
        self.assertIn("localStorage", js)
        self.assertIn("prefers-color-scheme", js)
        self.assertIn("ROUTES", js)
        for route in EXPECTED_ROUTES:
            self.assertIn(f'"{route}"', js)
        # legacy aliases preserved for backwards compat
        for alias in LEGACY_ALIASES:
            self.assertIn(f'"{alias}"', js)

    def test_js_handles_snapshot_validation_and_size(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("1048576", js)  # 1 MB limit
        self.assertIn("read_only", js)
        self.assertIn("Unsupported snapshot", js)

    def test_js_no_network_fetch(self):
        js = APP_JS.read_text(encoding="utf-8")
        # must not fetch from localhost or external CDN
        self.assertNotIn('fetch("http', js)
        self.assertNotIn("fetch('http", js)
        self.assertNotIn("localhost", js)
        self.assertNotIn("127.0.0.1", js)

    def test_live_invoice_issue_and_ledger_workspace_are_mounted(self):
        html = APP_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn('data-live-resource="invoices"', html)
        self.assertIn('data-live-resource="ledger"', html)
        self.assertRegex(js, r'invoices:\\{[^\\n]*actions:\\{draft:\\["issue"\\]\\}')
        self.assertIn('"ledger": {"kind", "amount_paise", "note"}', (ROOT / "ai_earn" / "workspace_api.py").read_text(encoding="utf-8"))

    def test_css_has_dark_and_light_themes(self):
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertIn("--bg", css)
        self.assertIn('[data-theme="light"]', css)
        self.assertIn("color-scheme", css)
        self.assertIn("@media", css)

    def test_css_has_responsive_breakpoints(self):
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertIn("max-width:820px", css)
        self.assertIn("max-width:560px", css)

    def test_index_redirects_to_spa(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("app.html", html)
        self.assertIn("connected.html", html)  # legacy link preserved

    def test_empty_error_loading_states_present(self):
        html = APP_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("No snapshot loaded", html)
        self.assertIn("empty", html.lower())
        self.assertIn("Loading snapshot", js)
        self.assertIn("Could not load snapshot", js)
        self.assertIn("No snapshot loaded", js)

    def test_aria_and_keyboard_accessible(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertIn('tabindex="-1"', html)  # main focusable
        self.assertIn('aria-labelledby', html)
        self.assertIn('role="banner"', html)

if __name__ == "__main__":
    unittest.main()
