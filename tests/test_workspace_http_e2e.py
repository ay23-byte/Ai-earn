"""HTTP-level end-to-end tests for the real local workspace server.

Uses disposable SQLite databases and synthetic accounts. No external provider,
live payment, existing user database, or network service is contacted.
"""
import json
import os
import http.client
from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.dashboard_metrics import DashboardMetrics
from ai_earn.local_server import EarnOSHTTPServer, EarnOSHandler, DASHBOARD_ROOT
from ai_earn.state_storage import LATEST_SCHEMA_VERSION, StateStorage


class WorkspaceHTTPClient:
    def __init__(self, port, username, password):
        self.port = port
        self.host = f"127.0.0.1:{port}"
        self.username = username
        self.password = password
        self.csrf = ""
        self.session = ""
        self.counter = 0

    @staticmethod
    def _cookie(headers, name):
        jar = SimpleCookie()
        for key, value in headers:
            if key.lower() == "set-cookie":
                jar.load(value)
        return jar[name].value if name in jar else ""

    def request(self, method, path, payload=None, *, authenticated=True, expected=200):
        headers = {"Host": self.host}
        if authenticated and self.session:
            headers["Cookie"] = f"ai_earn_session={self.session}"
        if self.csrf:
            cookie = headers.get("Cookie", "")
            headers["Cookie"] = (cookie + "; " if cookie else "") + f"ai_earn_csrf={self.csrf}"
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
            headers["Origin"] = "http://" + self.host
            headers["X-CSRF-Token"] = self.csrf
            self.counter += 1
            headers["Idempotency-Key"] = f"http-e2e-{self.username}-{self.counter:04d}"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            response_headers = response.getheaders()
            content_type = response.getheader("Content-Type", "")
            decoded = json.loads(raw) if raw and content_type.startswith("application/json") else raw
            if method == "GET" and path == "/api/v1/csrf":
                self.csrf = self._cookie(response_headers, "ai_earn_csrf")
            if method == "POST" and path == "/api/v1/auth/login" and response.status == 200:
                self.session = self._cookie(response_headers, "ai_earn_session")
            self.assert_status(response.status, expected, decoded)
            return decoded
        finally:
            connection.close()

    @staticmethod
    def assert_status(actual, expected, body):
        if actual != expected:
            raise AssertionError(f"HTTP {actual}, expected {expected}: {body}")

    def login(self):
        self.request("GET", "/api/v1/csrf", authenticated=False)
        return self.request(
            "POST",
            "/api/v1/auth/login",
            {"username": self.username, "password": self.password},
            authenticated=False,
        )

    def get(self, path, expected=200):
        return self.request("GET", path, expected=expected)

    def post(self, path, payload=None, expected=200):
        return self.request("POST", path, payload or {}, expected=expected)


class WorkspaceHTTPEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.private_dir = Path(self.temp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.private_dir)
        else:
            self.private_dir.mkdir(mode=0o700)

        iterations = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        iterations.start()
        self.addCleanup(iterations.stop)

        self.auth = AuthService(AuthStorage(self.private_dir / "auth.sqlite3"))
        self.auth.register("owner", "disposable e2e password")
        self.auth.register("other", "disposable e2e password")

        state_path = self.private_dir / "state.sqlite3"
        StateStorage(state_path, create=True, confirmed=True)
        for version in range(2, LATEST_SCHEMA_VERSION + 1):
            getattr(StateStorage(state_path), f"migrate_to_v{version}")(
                self.private_dir / f"before-v{version}.sqlite3",
                confirmed=True,
            )
        self.state = StateStorage(state_path)
        self.metrics = DashboardMetrics(self.auth, self.state)
        self.server = None
        self.thread = None
        self._start_server()
        self.owner = WorkspaceHTTPClient(self.server.server_port, "owner", "disposable e2e password")
        self.other = WorkspaceHTTPClient(self.server.server_port, "other", "disposable e2e password")
        self.owner.login()
        self.other.login()

    def _start_server(self):
        self.server = EarnOSHTTPServer(
            ("127.0.0.1", 0), EarnOSHandler, self.auth, self.state, self.metrics, DASHBOARD_ROOT
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.server = None
            self.thread = None

    def tearDown(self):
        self._stop_server()

    def test_browser_session_workspace_persistence_metrics_and_owner_isolation(self):
        # The actual dashboard shell and JavaScript are served by the same HTTP
        # server that handles the API; the session token stays in an HttpOnly cookie.
        shell = self.owner.get("/")
        self.assertIn(b"<!doctype html", shell.lower())
        app_js = self.owner.get("/app.js")
        self.assertIn(b"/api/v1/workspace/", app_js)

        opportunity_id = self.owner.post(
            "/api/v1/workspace/opportunities",
            {
                "title": "HTTP E2E website opportunity",
                "source_url": "https://example.com/disposable-opportunity",
                "budget_paise": 2500000,
                "risk_level": "low",
                "skill_tags": "html,css,python",
            },
            expected=201,
        )["data"]["id"]
        self.assertEqual(
            self.owner.get(f"/api/v1/workspace/opportunities/{opportunity_id}")["data"]["item"]["title"],
            "HTTP E2E website opportunity",
        )

        customer_id = self.owner.post(
            "/api/v1/workspace/customers",
            {"name": "Synthetic HTTP Client", "contact": "synthetic@example.invalid", "source": "manual"},
            expected=201,
        )["data"]["id"]
        service_id = self.owner.post(
            "/api/v1/workspace/services",
            {
                "title": "HTTP E2E service",
                "scope": "Synthetic scope",
                "deliverable": "Synthetic deliverable",
                "price_paise": 2500000,
                "revisions": 2,
            },
            expected=201,
        )["data"]["id"]
        self.owner.post(f"/api/v1/workspace/services/{service_id}/actions/publish")
        proposal_id = self.owner.post(
            "/api/v1/workspace/proposals",
            {
                "service_id": service_id,
                "customer_id": customer_id,
                "title": "Synthetic HTTP proposal",
                "description": "Disposable proposal for end-to-end verification.",
                "price_paise": 2500000,
                "delivery_days": 14,
                "ai_generated": False,
            },
            expected=201,
        )["data"]["id"]
        self.owner.post(f"/api/v1/workspace/proposals/{proposal_id}/actions/send")
        self.owner.post(f"/api/v1/workspace/proposals/{proposal_id}/actions/accept")

        order_id = self.owner.post(
            "/api/v1/workspace/orders", {"proposal_id": proposal_id}, expected=201
        )["data"]["id"]
        invoice_id = self.owner.post(
            "/api/v1/workspace/invoices",
            {"order_id": order_id, "billing_name": "Synthetic HTTP Client"},
            expected=201,
        )["data"]["id"]
        self.owner.post(f"/api/v1/workspace/invoices/{invoice_id}/actions/issue")

        payment_id = self.owner.post(
            "/api/v1/workspace/payments",
            {
                "amount_paise": 2500000,
                "provider": "sandbox",
                "provider_ref": "http-e2e-sandbox-reference",
                "invoice_id": invoice_id,
                "order_id": order_id,
            },
            expected=201,
        )["data"]["id"]
        # The status-transition endpoint must not be a shortcut for
        # marking a sandbox/provider payment verified.
        self.owner.post(
            f"/api/v1/workspace/payments/{payment_id}/actions/received",
            {"verified": 1},
            expected=400,
        )
        self.owner.post(f"/api/v1/workspace/payments/{payment_id}/actions/received")
        self.owner.post(f"/api/v1/workspace/payments/{payment_id}/actions/settled")
        payment = self.owner.get(f"/api/v1/workspace/payments/{payment_id}")["data"]["item"]
        self.assertEqual(payment["status"], "settled")
        self.assertEqual(payment["verified"], 0)
        self.owner.post(
            f"/api/v1/workspace/payments/{payment_id}/actions/verify",
            {"verified": 1},
            expected=400,
        )

        expense_id = self.owner.post(
            "/api/v1/workspace/expenses",
            {"amount_paise": 125000, "category": "Software", "note": "Synthetic test expense"},
            expected=201,
        )["data"]["id"]
        expense = self.owner.get(f"/api/v1/workspace/expenses/{expense_id}")["data"]["item"]
        self.assertEqual(expense["amount_paise"], 125000)
        self.assertTrue(
            any(item["id"] == expense_id for item in self.owner.get("/api/v1/workspace/expenses")["data"]["items"])
        )

        # A second signed-in account cannot read or list the first account's data.
        self.assertEqual(
            self.other.get(f"/api/v1/workspace/opportunities/{opportunity_id}", expected=404)["error"]["code"],
            "not_found",
        )
        self.assertFalse(
            any(item["id"] == customer_id for item in self.other.get("/api/v1/workspace/customers")["data"]["items"])
        )
        self.assertEqual(
            self.other.get(f"/api/v1/workspace/invoices/{invoice_id}", expected=404)["error"]["code"],
            "not_found",
        )

        metrics = self.owner.get("/api/v1/dashboard/metrics")["data"]["metrics"]
        self.assertEqual(metrics["opportunities"]["count"], 1)
        self.assertEqual(metrics["customers"]["count"], 1)
        self.assertEqual(metrics["income"]["verified_amount_paise"], 0)
        other_metrics = self.other.get("/api/v1/dashboard/metrics")["data"]["metrics"]
        self.assertEqual(other_metrics["opportunities"]["count"], 0)
        self.assertEqual(other_metrics["customers"]["count"], 0)

        # Restart only the HTTP server; keep the disposable SQLite files. Existing
        # authenticated sessions and created records must still work afterwards.
        self._stop_server()
        self._start_server()
        self.owner.port = self.server.server_port
        self.owner.host = f"127.0.0.1:{self.server.server_port}"
        self.other.port = self.server.server_port
        self.other.host = f"127.0.0.1:{self.server.server_port}"
        persisted = self.owner.get(f"/api/v1/workspace/invoices/{invoice_id}")["data"]["item"]
        self.assertEqual(persisted["status"], "issued")
        self.assertEqual(
            self.owner.get(f"/api/v1/workspace/payments/{payment_id}")["data"]["item"]["verified"],
            0,
        )
        self.assertEqual(self.owner.get("/api/v1/me")["data"]["account"]["username"], "owner")


if __name__ == "__main__":
    unittest.main()
