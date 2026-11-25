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

    def test_html_element_ids_are_unique_and_customer_activity_targets_are_separate(self):
        html = APP_HTML.read_text(encoding="utf-8")
        ids = re.findall(r'id="([^"]+)"', html)
        duplicates = sorted({element_id for element_id in ids if ids.count(element_id) > 1})
        self.assertEqual(duplicates, [], f"duplicate HTML element IDs: {duplicates}")
        self.assertIn('id="customerActivityDetail"', html)
        self.assertIn('id="analyticsCustomerActivityDetail"', html)
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn('const caEl2=$("analyticsCustomerActivityDetail")', js)

    def test_login_form_fails_closed_without_javascript_and_never_uses_get(self):
        html = APP_HTML.read_text(encoding="utf-8")
        form = re.search(r'<form\\b[^>]*id="loginForm"[^>]*>', html)
        self.assertIsNotNone(form, "login form must exist")
        markup = form.group(0)
        self.assertIn('method="post"', markup)
        self.assertIn('action="/api/v1/auth/login"', markup)
        # The inline cancellation is a fail-closed guard if app.js fails to load:
        # browser-native form submission must never serialize credentials into the URL.
        self.assertIn('onsubmit="return false;"', markup)

    def test_sign_in_handler_is_bound_before_startup_api_probes(self):
        js = APP_JS.read_text(encoding="utf-8")
        init_start = js.index("async function initLocalApi(){")
        handler = js.index('if(form) form.addEventListener("submit", async e=>{', init_start)
        startup_probe = js.index('const csrf=await apiJson("/api/v1/csrf")', init_start)
        self.assertLess(
            handler,
            startup_probe,
            "Sign-in must be clickable even if startup CSRF/session requests stall.",
        )

    def test_html_has_viewport_and_color_scheme(self):
        html = APP_HTML.read_text(encoding="utf-8")
        self.assertIn('name="viewport"', html)
        self.assertIn('name="color-scheme"', html)
        self.assertIn('content="dark light"', html)

    def test_ai_workspace_premium_hero_and_status_ids_are_unique(self):
        html = APP_HTML.read_text(encoding="utf-8")
        css = APP_CSS.read_text(encoding="utf-8")

        self.assertIn("Your AI workspace.", html)
        self.assertIn("Ideas into action.", html)
        self.assertIn('class="ai-orbit-scene"', html)
        self.assertIn('class="ai-status-grid"', html)
        self.assertIn("Consequential actions require a separate approval decision.", html)

        # These IDs are updated by app.js; duplicate IDs can silently target
        # the wrong status card after the legacy rows are removed.
        for element_id in (
            "aiProviderStatus",
            "aiProviderInfo",
            "aiCount",
            "aiStatus",
            "emergencyState",
            "emergencyStatus",
        ):
            with self.subTest(element_id=element_id):
                self.assertEqual(
                    len(re.findall(rf'id="{re.escape(element_id)}"', html)),
                    1,
                    f"{element_id} must appear exactly once",
                )

        self.assertIn(".ai-hero", css)
        self.assertIn(".ai-orbit-scene", css)
        self.assertIn(".ai-status-grid", css)
        self.assertRegex(css, r"@media\s*\(prefers-reduced-motion:\s*reduce\)")

    def test_ai_assistant_exposes_safe_local_provider_settings(self):
        html = APP_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn('id="aiProviderForm"', html)
        self.assertIn('id="aiProviderSelect"', html)
        self.assertIn('<option value="ollama">Local Ollama</option>', html)
        self.assertIn('value="http://127.0.0.1:11434"', html)
        self.assertIn('aiPost("/api/v1/ai/provider"', js)
        self.assertIn('provider==="ollama"', js)

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

    def test_js_uses_same_origin_api_without_hardcoded_host(self):
        js = APP_JS.read_text(encoding="utf-8")
        # must not fetch from localhost or external CDN
        self.assertNotIn('fetch("http', js)
        self.assertNotIn("fetch('http", js)
        self.assertNotIn("localhost", js)
        self.assertNotIn("127.0.0.1", js)
        self.assertIn('fetch(path, Object.assign({ credentials:"same-origin", cache:"no-store" }', js)

    def test_live_invoice_issue_and_ledger_workspace_are_mounted(self):
        html = APP_HTML.read_text(encoding="utf-8")
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn('data-live-resource="invoices"', html)
        self.assertIn('data-live-resource="ledger"', html)
        self.assertIn('invoices:{title:"Invoices",fields:[', js)
        self.assertIn('actions:{draft:["issue"]}', js)
        self.assertIn('"ledger": {"kind", "amount_paise", "note"}', (ROOT / "ai_earn" / "workspace_api.py").read_text(encoding="utf-8"))

    def test_live_workspace_uses_owner_scoped_linked_record_selectors(self):
        js = APP_JS.read_text(encoding="utf-8")
        # Foreign-key fields must be selected from live account records, not copied IDs.
        self.assertIn('projects:{title:"Client projects",fields:[["customer_id","Customer","select",true]', js)
        self.assertIn('["service_id","Service","select",true]', js)
        self.assertIn('["customer_id","Customer (optional)","select",false]', js)
        self.assertIn('["proposal_id","Accepted proposal","select",true]', js)
        self.assertIn('["order_id","Order","select",true]', js)
        self.assertIn('["invoice_id","Invoice (optional)","select",false]', js)
        self.assertIn('["order_id","Order (optional)","select",false]', js)
        self.assertIn('invoice_id: { resource:"invoices", empty:"Select an issued invoice" }', js)
        self.assertIn('field==="invoice_id"?options.filter(item=>item.status==="issued")', js)
        self.assertIn('Payment status changes do not prove money was received or settled.', js)
        self.assertIn('k==="verified"?(item[k]===1?"VERIFIED":"UNVERIFIED")', js)
        self.assertIn('field==="order_id" && formResource==="payments"?options.filter(item=>item.status!=="cancelled")', js)
        self.assertIn('const LIVE_SELECTS = {', js)
        self.assertIn('function populateLiveSelects()', js)
        self.assertIn('"Idempotency-Key":liveIdempotencyKey()', js)
        self.assertIn('liveRecordCache[resource]=rows;', js)
        self.assertIn('Object.keys(liveRecordCache).forEach(key=>delete liveRecordCache[key]);', js)
        self.assertIn('item.status==="accepted" && item.customer_id', js)
        self.assertIn('options.filter(item=>item.status==="draft")', js)
        self.assertIn('escapeHtml(liveSelectLabel(config.resource,item))', js)

    def test_customer_notes_and_requirements_live_workspace(self):
        js = APP_JS.read_text(encoding="utf-8")
        css = APP_CSS.read_text(encoding="utf-8")
        self.assertIn('data-live-customer-details=', js)
        self.assertIn('/notes', js)
        self.assertIn('/requirements', js)
        self.assertIn('data-customer-detail-kind', js)
        self.assertIn('X-CSRF-Token', js)
        self.assertIn('customer-detail-columns', css)
        self.assertIn('escapeHtml(item[key]||"")', js)

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
