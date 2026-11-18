"""Phase 6 — service workspace (v25)."""
import os
import tempfile, unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth_storage import AuthStorage
from ai_earn.auth import AuthService
from ai_earn.state_storage import StateStorage, LATEST_SCHEMA_VERSION
from ai_earn.services import Services, Portfolio
from ai_earn.customers import Customers
from ai_earn.service_workspace import (
    ServicePackages, ServiceRequirements, ServiceAttachments,
    Proposals, CustomerRequests, ServiceTemplates, PortfolioPresentation, ServiceEditor,
    SERVICE_TEMPLATES
)
from ai_earn.state_storage import _validate_schema, _check_integrity

class Clock:
    now = 1800000000
    def __call__(self): return self.now

class ServiceWorkspaceTests(unittest.TestCase):
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
        # APIs
        self.svc=Services(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.port=Portfolio(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.cust=Customers(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.editor=ServiceEditor(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.pkgs=ServicePackages(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.reqs=ServiceRequirements(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.atts=ServiceAttachments(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.props=Proposals(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.reqs_cust=CustomerRequests(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.tmpl=ServiceTemplates()
        self.pres=PortfolioPresentation(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))

    def test_service_create_and_edit_descriptions(self):
        sid=self.svc.create(self.token,"Orig Title","Orig scope","Orig deliverable", 100000, 2, operation_key="sw_svc1")
        # edit descriptions
        self.editor.edit_description(self.token, sid, operation_key="edit1", scope="Edited scope", deliverable="Edited deliverable")
        svc=self.svc.get(self.token, sid)
        self.assertEqual(svc["scope"], "Edited scope")
        self.assertEqual(svc["deliverable"], "Edited deliverable")
        # full update
        self.editor.update(self.token, sid, operation_key="edit2", title="New Title", price_paise=200000, revisions=3, delivery_days=7)
        svc2=self.svc.get(self.token, sid)
        self.assertEqual(svc2["title"], "New Title")
        self.assertEqual(svc2["price_paise"], 200000)
        self.assertEqual(svc2["revisions"], 3)
        # verify package created for delivery_days
        pkgs=self.pkgs.list(self.token, sid)
        self.assertTrue(any(p["delivery_days"]==7 for p in pkgs))
        # idempotent
        self.editor.update(self.token, sid, operation_key="edit2", title="New Title", price_paise=200000, revisions=3, delivery_days=7)
        # still same
        self.assertEqual(self.svc.get(self.token, sid)["title"], "New Title")

    def test_packages_pricing_delivery_revisions(self):
        sid=self.svc.create(self.token,"Pkg Service","Scope","Deliverable", 300000, 2, operation_key="pkg_svc")
        pid=self.pkgs.create(self.token, sid, "Basic", "Basic desc", 50000, 2, 1, operation_key="pkg_basic")
        self.assertEqual(self.pkgs.get(self.token, pid)["price_paise"], 50000)
        self.assertEqual(self.pkgs.get(self.token, pid)["delivery_days"], 2)
        self.assertEqual(self.pkgs.get(self.token, pid)["revisions"], 1)
        # update pricing
        self.pkgs.update(self.token, pid, operation_key="pkg_upd", price_paise=75000, delivery_days=3, revisions=2)
        upd=self.pkgs.get(self.token, pid)
        self.assertEqual(upd["price_paise"], 75000)
        self.assertEqual(upd["delivery_days"], 3)
        self.assertEqual(upd["revisions"], 2)
        # list
        pid2=self.pkgs.create(self.token, sid, "Premium", "Premium desc", 200000, 7, 3, operation_key="pkg_prem")
        lst=self.pkgs.list(self.token, sid)
        self.assertEqual(len(lst), 2)
        # delete
        self.pkgs.delete(self.token, pid, operation_key="pkg_del")
        lst2=self.pkgs.list(self.token, sid)
        self.assertEqual(len(lst2), 1)

    def test_requirements(self):
        sid=self.svc.create(self.token,"Req Service","Scope","Deliverable", 100000, 1, operation_key="req_svc")
        rid=self.reqs.add(self.token, sid, "Provide source data in Excel", operation_key="req_add1")
        rid2=self.reqs.add(self.token, sid, "Include formatting rules", operation_key="req_add2")
        lst=self.reqs.list(self.token, sid)
        self.assertEqual(len(lst), 2)
        self.reqs.remove(self.token, rid, operation_key="req_del")
        self.assertEqual(len(self.reqs.list(self.token, sid)), 1)

    def test_portfolio_and_attachments(self):
        sid=self.svc.create(self.token,"Port Service","Scope","Deliverable", 100000, 1, operation_key="port_svc")
        port_id=self.port.create(self.token, sid, "Portfolio Title", "Portfolio desc", operation_key="port_item1")
        # service attachment
        aid=self.atts.add(self.token, operation_key="att_svc", file_name="spec.pdf", file_size=1024, mime_type="application/pdf", service_id=sid, file_hash=b"\x00"*32)
        self.assertEqual(len(self.atts.list(self.token, service_id=sid)), 1)
        # portfolio attachment
        aid2=self.atts.add(self.token, operation_key="att_port", file_name="screenshot.png", file_size=2048, mime_type="image/png", portfolio_item_id=port_id)
        self.assertEqual(len(self.atts.list(self.token, portfolio_item_id=port_id)), 1)
        # remove
        self.atts.remove(self.token, aid, operation_key="att_del")
        self.assertEqual(len(self.atts.list(self.token, service_id=sid)), 0)

    def test_proposals_and_ai_draft(self):
        sid=self.svc.create(self.token,"Prop Service","Scope","Deliverable for proposal", 150000, 2, operation_key="prop_svc")
        cid=self.cust.create(self.token, "Bob", "bob@example.com", "ref", operation_key="prop_cust")
        pid=self.props.create(self.token, sid, "Proposal Title", "Proposal desc", 150000, 5, operation_key="prop_create", customer_id=cid)
        self.assertEqual(self.props.get(self.token, pid)["status"], "draft")
        self.assertEqual(self.props.get(self.token, pid)["ai_generated"], 0)
        # AI draft
        pid_ai=self.props.generate_ai_draft(self.token, sid, operation_key="prop_ai", customer_id=cid, prompt="Create AI proposal")
        self.assertEqual(self.props.get(self.token, pid_ai)["ai_generated"], 1)
        self.assertIn("Proposal", self.props.get(self.token, pid_ai)["title"])
        # status tracking
        self.props.send(self.token, pid, operation_key="prop_send")
        self.assertEqual(self.props.get(self.token, pid)["status"], "sent")
        self.props.accept(self.token, pid, operation_key="prop_accept")
        self.assertEqual(self.props.get(self.token, pid)["status"], "accepted")
        # list filter
        drafts=self.props.list(self.token, status="draft")
        self.assertTrue(any(p["id"]==pid_ai for p in drafts))

    def test_proposal_status_transitions_and_idempotency(self):
        sid=self.svc.create(self.token,"Prop2 Service","Scope","Deliverable", 100000, 1, operation_key="prop2_svc")
        pid=self.props.create(self.token, sid, "Title", "Desc", 100000, 3, operation_key="prop2_create")
        # idempotent create
        pid_dup=self.props.create(self.token, sid, "Title", "Desc", 100000, 3, operation_key="prop2_create")
        self.assertEqual(pid, pid_dup)
        # invalid transition: draft -> accepted should fail
        with self.assertRaises(Exception):
            self.props.accept(self.token, pid, operation_key="prop2_bad")
        # draft -> sent -> rejected -> archived
        self.props.send(self.token, pid, operation_key="prop2_send")
        self.props.reject(self.token, pid, operation_key="prop2_reject")
        self.props.archive(self.token, pid, operation_key="prop2_arch")
        self.assertEqual(self.props.get(self.token, pid)["status"], "archived")
        # idempotent transition
        self.props.archive(self.token, pid, operation_key="prop2_arch")
        # operation key reuse with different target should fail
        with self.assertRaises(Exception):
            self.props.create(self.token, sid, "Other Title", "Other", 200000, 5, operation_key="prop2_create")

    def test_customer_requests(self):
        sid=self.svc.create(self.token,"ReqSvc","Scope","Deliverable", 100000, 1, operation_key="creq_svc")
        cid=self.cust.create(self.token, "AliceCust","alicecust@example.com","ref", operation_key="creq_cust")
        rid=self.reqs_cust.create(self.token, sid, cid, "Please help with data", operation_key="creq_create")
        self.assertEqual(self.reqs_cust.get(self.token, rid)["status"], "pending")
        self.reqs_cust.accept(self.token, rid, operation_key="creq_accept")
        self.assertEqual(self.reqs_cust.get(self.token, rid)["status"], "accepted")
        self.reqs_cust.complete(self.token, rid, operation_key="creq_comp")
        self.assertEqual(self.reqs_cust.get(self.token, rid)["status"], "completed")
        # list
        lst=self.reqs_cust.list(self.token, service_id=sid)
        self.assertEqual(len(lst), 1)
        # another request
        rid2=self.reqs_cust.create(self.token, sid, cid, "Another request", operation_key="creq_create2")
        self.reqs_cust.reject(self.token, rid2, operation_key="creq_rej")
        self.assertEqual(self.reqs_cust.get(self.token, rid2)["status"], "rejected")

    def test_templates_editable_not_hardcoded(self):
        # list 9 templates
        names=self.tmpl.list_templates()
        self.assertEqual(len(names), 9)
        self.assertIn("excel_data_cleaning", names)
        self.assertIn("python_automation", names)
        # get template and verify editable
        tmpl=self.tmpl.get_template("excel_data_cleaning")
        original_title=tmpl["title"]
        # create from template with override
        sid=self.tmpl.create_service_from_template(
            AuthService(AuthStorage(self.auth_path), self.clock),
            StateStorage(self.state_path),
            self.token, "excel_data_cleaning", operation_key="tmpl_create", title="Custom Excel Service", price_paise=99999
        )
        svc=self.svc.get(self.token, sid)
        self.assertEqual(svc["title"], "Custom Excel Service")
        self.assertEqual(svc["price_paise"], 99999)
        self.assertNotEqual(svc["title"], original_title)  # edited
        # verify packages and requirements created
        pkgs=self.pkgs.list(self.token, sid)
        self.assertGreaterEqual(len(pkgs), 3)
        reqs=self.reqs.list(self.token, sid)
        self.assertGreaterEqual(len(reqs), 2)
        # edit after creation
        self.editor.update(self.token, sid, operation_key="tmpl_edit", scope="Edited after template")
        self.assertEqual(self.svc.get(self.token, sid)["scope"], "Edited after template")
        # ensure template itself unchanged (editable copy, not hard limit)
        tmpl2=self.tmpl.get_template("excel_data_cleaning")
        self.assertEqual(tmpl2["title"], original_title)
        # all 9 templates can be instantiated and edited
        for tid in names:
            sid2=self.tmpl.create_service_from_template(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path), self.token, tid, operation_key=f"tmpl_{tid}_op", title=f"Edited {tid}")
            self.assertEqual(self.svc.get(self.token, sid2)["title"], f"Edited {tid}")
            # verify not hardcoded: change scope
            self.editor.update(self.token, sid2, operation_key=f"tmpl_{tid}_edit", scope=f"Custom scope for {tid}")
            self.assertEqual(self.svc.get(self.token, sid2)["scope"], f"Custom scope for {tid}")

    def test_portfolio_presentation_professional(self):
        # create service with portfolio and attachments
        sid=self.svc.create(self.token,"Pres Service","Analysis scope","Analysis deliverable", 300000, 2, operation_key="pres_svc")
        pkgs=self.pkgs.create(self.token, sid, "Standard", "Standard package", 300000, 5, 2, operation_key="pres_pkg")
        reqs=self.reqs.add(self.token, sid, "Dataset required", operation_key="pres_req")
        port_id=self.port.create(self.token, sid, "Case Study: Sales Dashboard", "Built interactive dashboard with filters", operation_key="pres_port")
        self.atts.add(self.token, operation_key="pres_att1", file_name="dashboard.pdf", file_size=5000, mime_type="application/pdf", service_id=sid, file_hash=b"\x01"*32)
        self.atts.add(self.token, operation_key="pres_att2", file_name="preview.png", file_size=3000, mime_type="image/png", portfolio_item_id=port_id)
        pres=self.pres.render(self.token, sid)
        self.assertEqual(pres["total_services"], 1)
        svc_data=pres["services"][0]
        self.assertEqual(svc_data["service"]["id"], sid)
        self.assertGreaterEqual(len(svc_data["packages"]), 1)
        self.assertGreaterEqual(len(svc_data["requirements"]), 1)
        self.assertGreaterEqual(len(svc_data["attachments"]), 1)
        self.assertEqual(len(svc_data["portfolio_items"]), 1)
        self.assertIn("headline", svc_data["presentation"])
        self.assertIn("price_display", svc_data["presentation"])
        self.assertEqual(svc_data["presentation"]["verification"], "unverified")
        # render all
        all_pres=self.pres.render(self.token)
        self.assertGreaterEqual(all_pres["total_services"], 1)

    def test_account_scoping_and_idempotency(self):
        sid=self.svc.create(self.token,"Scope Service","Scope","Deliverable", 100000, 1, operation_key="scope_svc")
        # other owner
        auth2=AuthService(AuthStorage(self.auth_path), self.clock)
        auth2.register("bob","other password safe")
        token2=auth2.login("bob","other password safe")
        svc2=Services(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        pkgs2=ServicePackages(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        # bob cannot see alice's service
        with self.assertRaises(Exception):
            svc2.get(token2, sid)
        # bob can create same operation_key (per-owner)
        sid2=svc2.create(token2, "Scope Service", "Scope", "Deliverable", 100000, 1, operation_key="scope_svc")
        self.assertNotEqual(sid, sid2)
        # idempotent within owner
        sid_dup=self.svc.create(self.token, "Scope Service", "Scope", "Deliverable", 100000, 1, operation_key="scope_svc")
        self.assertEqual(sid, sid_dup)
        # package idempotency
        pid=self.pkgs.create(self.token, sid, "Basic", "Desc", 50000, 2, 1, operation_key="scope_pkg")
        pid_dup=self.pkgs.create(self.token, sid, "Basic", "Desc", 50000, 2, 1, operation_key="scope_pkg")
        self.assertEqual(pid, pid_dup)
        # operation key reuse with different inputs should fail
        with self.assertRaises(Exception):
            self.pkgs.create(self.token, sid, "Basic2", "Desc2", 60000, 3, 2, operation_key="scope_pkg")

    def test_migrations_and_integrity(self):
        self.assertEqual(LATEST_SCHEMA_VERSION, 29)
        with StateStorage(self.state_path).transaction() as conn:
            self.assertEqual(_validate_schema(conn), LATEST_SCHEMA_VERSION)
            _check_integrity(conn)
        count=len(list(self.priv.glob("before-v*.sqlite3")))
        self.assertEqual(count, LATEST_SCHEMA_VERSION-1)

if __name__=="__main__":
    unittest.main()

