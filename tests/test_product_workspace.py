"""Phase 7 — digital product workspace (v26)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth_storage import AuthStorage
from ai_earn.auth import AuthService
from ai_earn.state_storage import StateStorage, LATEST_SCHEMA_VERSION, _validate_schema, _check_integrity
from ai_earn.products import Products
from ai_earn.product_workspace import (
    ProductIdeas, ProductValidations, MarketResearch, PricingSuggestions,
    ProductVersions, ProductFiles, ProductDeliverables, ProductSales,
    ProductDocumentation, ProductEditor
)

class Clock:
    now=1800000000
    def __call__(self): return self.now

class ProductWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.priv=Path(self.tmp.name)/"priv"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.priv)
        else:
            self.priv.mkdir(mode=0o700)
        self.clock=Clock()
        patcher=patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth_path=self.priv/"auth.sqlite3"
        self.state_path=self.priv/"state.sqlite3"
        self.auth=AuthService(AuthStorage(self.auth_path, create=True), self.clock)
        self.auth.register("alice","disposable test password")
        self.token=self.auth.login("alice","disposable test password")
        self.state=StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION+1):
            getattr(self.state, f"migrate_to_v{v}")(self.priv/f"before-v{v}.sqlite3", confirmed=True)
        self.prod=Products(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.ideas=ProductIdeas(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.vals=ProductValidations(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.research=MarketResearch(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.pricing=PricingSuggestions(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.versions=ProductVersions(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.files=ProductFiles(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.deliver=ProductDeliverables(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.sales=ProductSales(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.docs=ProductDocumentation(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.editor=ProductEditor(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))

    def test_product_ideas_and_validation(self):
        iid=self.ideas.create(self.token, "Idea Title", "Idea desc", operation_key="idea1")
        self.assertEqual(self.ideas.get(self.token, iid)["status"], "idea")
        self.ideas.validate(self.token, iid, operation_key="idea_val")
        self.assertEqual(self.ideas.get(self.token, iid)["status"], "validated")
        # idempotent
        iid2=self.ideas.create(self.token, "Idea Title", "Idea desc", operation_key="idea1")
        self.assertEqual(iid, iid2)
        # validation linked to product
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="prod_val")
        self.vals.add(self.token, pid, "Criteria: demand", "pass", operation_key="val1", evidence="Survey 10 users")
        lst=self.vals.list(self.token, pid)
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["result"], "pass")

    def test_product_descriptions_and_editor(self):
        pid=self.prod.create(self.token, "Prod", "Old desc", "v1", 100000, operation_key="prod_desc")
        self.editor.update(self.token, pid, operation_key="upd_desc", description="New detailed description with features")
        self.assertEqual(self.prod.get(self.token, pid)["description"], "New detailed description with features")
        self.editor.update(self.token, pid, operation_key="upd_title", title="Renamed Product")
        self.assertEqual(self.prod.get(self.token, pid)["title"], "Renamed Product")
        # idempotent
        self.editor.update(self.token, pid, operation_key="upd_title", title="Renamed Product")
        self.assertEqual(self.prod.get(self.token, pid)["title"], "Renamed Product")

    def test_pricing_suggestions_and_ai(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 200000, operation_key="price_prod")
        self.pricing.add(self.token, pid, 250000, "Competitor analysis", operation_key="price_sug", evidence="Competitor price 300000")
        self.assertEqual(self.pricing.list(self.token, pid)[0]["price_paise"], 250000)
        # AI suggestion
        ps_ai=self.pricing.generate_ai_suggestion(self.token, pid, operation_key="price_ai")
        lst=self.pricing.list(self.token, pid)
        ai_entry=next(r for r in lst if r["id"]==ps_ai)
        self.assertEqual(ai_entry["ai_generated"], 1)
        self.assertIn("unverified", ai_entry["rationale"].lower())

    def test_product_versions(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="ver_prod")
        self.versions.add(self.token, pid, "v2", "Added feature", operation_key="ver1")
        self.assertEqual(self.versions.list(self.token, pid)[0]["version"], "v2")
        # product version updated
        self.assertEqual(self.prod.get(self.token, pid)["version"], "v2")
        # second version
        self.versions.add(self.token, pid, "v3", "Major update", operation_key="ver2")
        self.assertEqual(len(self.versions.list(self.token, pid)), 2)

    def test_file_management(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="file_prod")
        fid=self.files.add(self.token, pid, "spec.pdf", 1024, "application/pdf", operation_key="file1", file_hash=b"\x00"*32)
        self.assertEqual(len(self.files.list(self.token, pid)), 1)
        self.files.remove(self.token, fid, operation_key="file_del")
        self.assertEqual(len(self.files.list(self.token, pid)), 0)
        # idempotent add
        fid2=self.files.add(token=self.token, product_id=pid, file_name="spec.pdf", file_size=1024, mime_type="application/pdf", operation_key="file1_again", file_hash=b"\x00"*32)
        self.assertNotEqual(fid, fid2)

    def test_product_status(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="status_prod")
        self.assertEqual(self.prod.get(self.token, pid)["status"], "draft")
        self.prod.publish(self.token, pid, operation_key="pub")
        self.assertEqual(self.prod.get(self.token, pid)["status"], "published")
        self.prod.archive(self.token, pid, operation_key="arch")
        self.assertEqual(self.prod.get(self.token, pid)["status"], "archived")

    def test_sales_tracking_no_fabrication(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="sale_prod")
        # unverified sale without evidence allowed
        self.sales.record(self.token, pid, 1, 100000, 1700000000, operation_key="sale_unver", evidence_url="", verification="unverified")
        self.assertEqual(self.sales.list(self.token, pid)[0]["verification"], "unverified")
        # verified without evidence must fail
        with self.assertRaises(Exception):
            self.sales.record(self.token, pid, 1, 100000, 1700000000, operation_key="sale_bad", verification="verified")
        # verified with evidence allowed
        sid2=self.sales.record(self.token, pid, 2, 200000, 1700000000, operation_key="sale_ver", evidence_url="https://example.com/receipt", verification="verified")
        lst=self.sales.list(self.token, pid)
        ver_entry=next(r for r in lst if r["id"]==sid2)
        self.assertEqual(ver_entry["verification"], "verified")
        summary=self.sales.summary(self.token, pid)
        self.assertEqual(summary["total_quantity"], 3)
        self.assertEqual(summary["verified_quantity"], 2)
        self.assertEqual(summary["total_sales"], 2)
        # Do not claim sales without evidence: summary verification remains unverified overall
        self.assertEqual(summary["verification"], "unverified")

    def test_downloadable_deliverables(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="del_prod")
        did=self.deliver.add(self.token, pid, "product.zip", 5000, "application/zip", operation_key="del1", file_hash=b"\x01"*32)
        self.assertEqual(len(self.deliver.list(self.token, pid)), 1)
        self.deliver.remove(self.token, did, operation_key="del_del")
        self.assertEqual(len(self.deliver.list(self.token, pid)), 0)

    def test_product_documentation_and_ai(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="doc_prod")
        docid=self.docs.create(self.token, pid, "Manual", "Content here", operation_key="doc1")
        self.assertEqual(self.docs.get(self.token, docid)["title"], "Manual")
        # AI draft
        doc_ai=self.docs.generate_ai_draft(self.token, pid, operation_key="doc_ai")
        self.assertIn("Docs:", self.docs.get(self.token, doc_ai)["title"])
        self.assertGreater(len(self.docs.list(self.token, pid)), 1)
        # update
        self.docs.update(self.token, docid, operation_key="doc_upd", content="Updated content")
        self.assertEqual(self.docs.get(self.token, docid)["content"], "Updated content")

    def test_market_research_and_ai_no_fabrication(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="res_prod")
        # research without evidence must be unverified
        self.research.add(self.token, pid, "Market query", "Findings data", operation_key="res1", evidence_url="", verification="unverified")
        self.assertEqual(self.research.list(self.token, pid)[0]["verification"], "unverified")
        # verified requires evidence
        with self.assertRaises(Exception):
            self.research.add(self.token, pid, "Q2", "F2", operation_key="res2", verification="verified")
        # verified with evidence ok
        rid2=self.research.add(self.token, pid, "Query2", "Findings2", operation_key="res2v", evidence_url="https://example.com/research", verification="verified")
        lst=self.research.list(self.token, pid)
        self.assertTrue(any(r["id"]==rid2 for r in lst))
        # AI draft is unverified and ai_generated
        rid_ai=self.research.generate_ai_draft(self.token, pid, "AI market query", operation_key="res_ai", evidence_url="")
        lst=self.research.list(self.token, pid)
        ai_entry=[r for r in lst if r["id"]==rid_ai][0]
        self.assertEqual(ai_entry["ai_generated"], 1)
        self.assertEqual(ai_entry["verification"], "unverified")
        # Do not fabricate: AI findings must be marked unverified even if evidence provided? Our implementation keeps unverified
        rid_ai2=self.research.generate_ai_draft(self.token, pid, "Another query", operation_key="res_ai2", evidence_url="https://example.com/evidence")
        # Still unverified because AI draft is always unverified
        self.assertEqual([r for r in self.research.list(self.token, pid) if r["id"]==rid_ai2][0]["verification"], "unverified")

    def test_account_scoping_and_idempotency(self):
        pid=self.prod.create(self.token, "Prod", "Desc", "v1", 100000, operation_key="scope_prod")
        # other owner
        auth2=AuthService(AuthStorage(self.auth_path), self.clock)
        auth2.register("bob","other password safe")
        token2=auth2.login("bob","other password safe")
        # bob cannot see alice's product
        with self.assertRaises(Exception):
            self.prod.get(token2, pid)
        # bob can create same operation_key
        prod2=Products(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        pid2=prod2.create(token2, "Prod", "Desc", "v1", 100000, operation_key="scope_prod")
        self.assertNotEqual(pid, pid2)
        # idempotent within owner
        ideas2=ProductIdeas(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        iid=ideas2.create(token2, "Idea", "Desc", operation_key="idea_scope")
        iid_dup=ideas2.create(token2, "Idea", "Desc", operation_key="idea_scope")
        self.assertEqual(iid, iid_dup)
        # operation key reuse with different inputs should fail
        with self.assertRaises(Exception):
            ideas2.create(token2, "Other Ideas", "Other", operation_key="idea_scope")

    def test_migrations_and_integrity(self):
        self.assertEqual(LATEST_SCHEMA_VERSION, 29)
        with StateStorage(self.state_path).transaction() as conn:
            self.assertEqual(_validate_schema(conn), LATEST_SCHEMA_VERSION)
            _check_integrity(conn)
        self.assertEqual(len(list(self.priv.glob("before-v*.sqlite3"))), LATEST_SCHEMA_VERSION-1)

if __name__=="__main__":
    unittest.main()

