"""Real SQLite-backed workspace API workflow regression tests.

These tests exercise the authenticated API facade and owner-scoped domain
services together. All records and payment references are disposable; no
external provider is contacted and no payment is marked received or verified.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import LATEST_SCHEMA_VERSION, StateStorage
from ai_earn.workspace_api import WorkspaceAPI


class WorkspaceAPIBusinessFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.private_dir = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory

            create_private_directory(self.private_dir)
        else:
            self.private_dir.mkdir(mode=0o700)

        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.auth = AuthService(AuthStorage(self.private_dir / "auth.sqlite3"))
        self.auth.register("owner", "disposable test password")
        self.auth.register("other", "disposable test password")
        self.owner_token = self.auth.login("owner", "disposable test password")
        self.other_token = self.auth.login("other", "disposable test password")

        state_path = self.private_dir / "state.sqlite3"
        state = StateStorage(state_path, create=True, confirmed=True)
        for version in range(2, LATEST_SCHEMA_VERSION + 1):
            getattr(state, f"migrate_to_v{version}")(
                self.private_dir / f"before-v{version}.sqlite3",
                confirmed=True,
            )
        self.state = StateStorage(state_path)
        self.api = WorkspaceAPI(self.auth, self.state)

    def request(self, method, path, *, payload=None, key=None, token=None, expected=200):
        status, body = self.api.dispatch(
            method,
            path,
            self.owner_token if token is None else token,
            payload or {},
            operation_key=key,
        )
        self.assertEqual(status, expected, body)
        self.assertEqual(body["ok"], 200 <= status < 300, body)
        return body["data"]

    def test_opportunity_to_accepted_proposal_to_issued_invoice_and_unverified_payment(self):
        opportunity_id = self.request(
            "POST",
            "/api/v1/workspace/opportunities",
            payload={
                "title": "Build a small business website",
                "source_url": "https://example.com/opportunities/website",
                "budget_paise": 2500000,
                "risk_level": "low",
                "skill_tags": "html, css, python",
            },
            key="flow-opportunity-create",
            expected=201,
        )["id"]
        self.request(
            "POST",
            f"/api/v1/workspace/opportunities/{opportunity_id}/actions/vetted",
            key="flow-opportunity-vet",
        )
        self.request(
            "POST",
            f"/api/v1/workspace/opportunities/{opportunity_id}/actions/pursuing",
            key="flow-opportunity-pursue",
        )

        service_id = self.request(
            "POST",
            "/api/v1/workspace/services",
            payload={
                "title": "Responsive business website",
                "scope": "Design and build a responsive five-page website",
                "deliverable": "Deployed website source and handover notes",
                "price_paise": 2500000,
                "revisions": 2,
            },
            key="flow-service-create",
            expected=201,
        )["id"]
        self.request(
            "POST",
            f"/api/v1/workspace/services/{service_id}/actions/publish",
            key="flow-service-publish",
        )

        customer_id = self.request(
            "POST",
            "/api/v1/workspace/customers",
            payload={
                "name": "Example Client",
                "contact": "client@example.com",
                "source": "manual",
            },
            key="flow-customer-create",
            expected=201,
        )["id"]

        project = self.request(
            "POST",
            "/api/v1/workspace/projects",
            payload={
                "customer_id": customer_id,
                "title": "Example Client website project",
                "description": "Track delivery of the accepted website scope.",
                "priority": 5,
            },
            key="flow-project-create",
            expected=201,
        )
        project_id = project["task_id"]
        projects = self.request("GET", "/api/v1/workspace/projects")["items"]
        self.assertTrue(
            any(
                item["task_id"] == project_id
                and item["customer_id"] == customer_id
                and item["id"] == project["link_id"]
                for item in projects
            ),
            "The project list should expose the linked task and customer relationship.",
        )

        proposal_id = self.request(
            "POST",
            "/api/v1/workspace/proposals",
            payload={
                "service_id": service_id,
                "customer_id": customer_id,
                "title": "Website development proposal",
                "description": "A responsive business website with two revision rounds.",
                "price_paise": 2500000,
                "delivery_days": 14,
                "ai_generated": False,
            },
            key="flow-proposal-create",
            expected=201,
        )["id"]
        draft_proposal_id = self.request(
            "POST",
            "/api/v1/workspace/proposals",
            payload={
                "service_id": service_id,
                "customer_id": customer_id,
                "title": "Draft proposal must not create an order",
                "description": "This proposal has not been accepted.",
                "price_paise": 2500000,
                "delivery_days": 14,
                "ai_generated": False,
            },
            key="flow-draft-proposal-create",
            expected=201,
        )["id"]
        self.request(
            "POST",
            "/api/v1/workspace/orders",
            payload={"proposal_id": draft_proposal_id},
            key="flow-order-from-draft-proposal",
            expected=400,
        )
        self.request(
            "POST",
            f"/api/v1/workspace/proposals/{proposal_id}/actions/send",
            key="flow-proposal-send",
        )
        self.request(
            "POST",
            f"/api/v1/workspace/proposals/{proposal_id}/actions/accept",
            key="flow-proposal-accept",
        )

        order_id = self.request(
            "POST",
            "/api/v1/workspace/orders",
            payload={"proposal_id": proposal_id},
            key="flow-order-create",
            expected=201,
        )["id"]
        order = self.request("GET", f"/api/v1/workspace/orders/{order_id}")["item"]
        self.assertEqual(order["proposal_id"], proposal_id)
        self.assertEqual(order["customer_id"], customer_id)
        self.assertEqual(order["proposed_amount_paise"], 2500000)
        self.assertEqual(order["title"], "Website development proposal")
        self.request(
            "POST",
            "/api/v1/workspace/orders",
            payload={"proposal_id": proposal_id},
            key="flow-order-duplicate",
            expected=400,
        )
        self.request(
            "GET",
            f"/api/v1/workspace/orders/{order_id}",
            token=self.other_token,
            expected=404,
        )
        invoice_id = self.request(
            "POST",
            "/api/v1/workspace/invoices",
            payload={
                "order_id": order_id,
                "billing_name": "Example Client",
                "billing_contact": "client@example.com",
            },
            key="flow-invoice-create",
            expected=201,
        )["id"]

        # Invoice issue is an explicit local action; it does not collect money.
        self.request(
            "POST",
            f"/api/v1/workspace/invoices/{invoice_id}/actions/issue",
            key="flow-invoice-issue",
        )
        invoice = self.request("GET", f"/api/v1/workspace/invoices/{invoice_id}")["item"]
        self.assertEqual(invoice["status"], "issued")
        self.assertIsNotNone(invoice["pdf_hash"])

        payment_id = self.request(
            "POST",
            "/api/v1/workspace/payments",
            payload={
                "amount_paise": 2500000,
                "provider": "sandbox",
                "provider_ref": "disposable-flow-reference",
                "invoice_id": invoice_id,
                "verified": 0,
            },
            key="flow-payment-create",
            expected=201,
        )["id"]
        payment = self.request("GET", f"/api/v1/workspace/payments/{payment_id}")["item"]
        self.assertEqual(payment["status"], "initiated")
        self.assertEqual(payment["verified"], 0)
        self.assertEqual(payment["provider"], "sandbox")

        # The full workflow remains private to its owner.
        hidden = self.request(
            "GET",
            f"/api/v1/workspace/invoices/{invoice_id}",
            token=self.other_token,
            expected=404,
        )
        self.assertIsNone(hidden)

        opportunity = self.request(
            "GET", f"/api/v1/workspace/opportunities/{opportunity_id}"
        )["item"]
        proposal = self.request(
            "GET", f"/api/v1/workspace/proposals/{proposal_id}"
        )["item"]
        self.assertEqual(opportunity["status"], "pursuing")
        self.assertEqual(proposal["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
