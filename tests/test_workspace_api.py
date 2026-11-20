import unittest
from ai_earn.workspace_api import WorkspaceAPI

class StubDomain:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "list": return [{"id": "record-1", "title": "Example"}]
            if name == "get": return {"id": args[1], "title": "Example"}
            return "record-1"
        return call

class WorkspaceAPITests(unittest.TestCase):
    def setUp(self):
        self.api = WorkspaceAPI.__new__(WorkspaceAPI)
        self.domain = StubDomain()
        for name in ("opportunities","services","service_editor","proposals","customers","products","tasks","orders","invoices","payments","expenses","ledger"):
            setattr(self.api, name, self.domain)

    def test_create_delegates_to_domain_and_passes_idempotency_key(self):
        status, body = self.api.dispatch("POST", "/api/v1/workspace/services", "session",
            {"title":"Excel cleanup","scope":"CSV cleanup","deliverable":"Clean workbook","price_paise":150000,"revisions":2},
            operation_key="client-key-123")
        self.assertEqual(status, 201)
        self.assertEqual(body["data"]["id"], "record-1")
        name, args, kwargs = self.domain.calls[-1]
        self.assertEqual((name, args), ("create", ("session",)))
        self.assertEqual(kwargs["operation_key"], "client-key-123")

    def test_list_returns_domain_rows_and_count(self):
        status, body = self.api.dispatch("GET", "/api/v1/workspace/services", "session", query="limit=20")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["items"][0]["id"], "record-1")
        self.assertEqual(body["data"]["count"], 1)

    def test_rejects_unknown_fields_before_calling_domain(self):
        before = len(self.domain.calls)
        status, body = self.api.dispatch("POST", "/api/v1/workspace/customers", "session",
            {"name":"Alice","contact":"alice@example.com","source":"manual","owner_id":"victim"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertEqual(len(self.domain.calls), before)

    def test_action_delegates_to_existing_state_machine(self):
        status, body = self.api.dispatch("POST", "/api/v1/workspace/products/product-1/actions/publish",
            "session", {}, operation_key="publish-key-123")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["action"], "publish")
        name, args, kwargs = self.domain.calls[-1]
        self.assertEqual((name, args), ("publish", ("session","product-1")))
        self.assertEqual(kwargs["operation_key"], "publish-key-123")

    def test_service_patch_uses_service_editor(self):
        status, body = self.api.dispatch("PATCH", "/api/v1/workspace/services/service-1",
            "session", {"title":"Updated"}, operation_key="update-key-123")
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["updated"])
        name, args, kwargs = self.domain.calls[-1]
        self.assertEqual((name, args), ("update", ("session","service-1")))
        self.assertEqual(kwargs["title"], "Updated")

    def test_unimplemented_resources_are_not_exposed(self):
        status, body = self.api.dispatch("POST", "/api/v1/workspace/security", "session", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

if __name__ == "__main__": unittest.main()
