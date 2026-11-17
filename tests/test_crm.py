"""Phase 8 — CRM (v27)."""
import os
import tempfile, unittest, time
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth_storage import AuthStorage
from ai_earn.auth import AuthService
from ai_earn.state_storage import StateStorage, LATEST_SCHEMA_VERSION, _validate_schema, _check_integrity
from ai_earn.customers import Customers
from ai_earn.communications import Communications
from ai_earn.followups import Followups
from ai_earn.services import Services
from ai_earn.service_workspace import Proposals
from ai_earn.project_tasks import ProjectTasks
from ai_earn.crm import (
    CustomerNotes, CustomerRequirements, CustomerOrders, CustomerProjects,
    CommunicationApprovals, OutboundWorkflowConfig, CRMCommunications,
    ActivityTimeline, CustomerProfile, CustomerCSV, CRMError
)

class Clock:
    now=1800000000
    def __call__(self): return self.now

class CRMTests(unittest.TestCase):
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
        # fresh services per test
        self.cust = Customers(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.comm = Communications(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.foll = Followups(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.notes = CustomerNotes(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.reqs = CustomerRequirements(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.orders = CustomerOrders(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.projects = CustomerProjects(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.approvals = CommunicationApprovals(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.outbound = OutboundWorkflowConfig(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.crm_comms = CRMCommunications(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.timeline = ActivityTimeline(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.profile = CustomerProfile(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.csv = CustomerCSV(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.pt = ProjectTasks(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.svc = Services(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))
        self.prop = Proposals(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path))

    def _customer(self):
        return self.cust.create(self.token, "Acme", "acme@example.com", "web", operation_key=f"cust_{secrets_token()}")


def secrets_token():
    import secrets as s
    return s.token_hex(8)

class CRMTestsFull(CRMTests):
    def test_customer_notes_and_requirements(self):
        cid=self.cust.create(self.token, "Bob", "bob@example.com", "ads", operation_key="c1")
        nid=self.notes.add(self.token, cid, "First note content", operation_key="n1")
        self.assertEqual(len(self.notes.list(self.token, cid)),1)
        self.assertEqual(self.notes.get(self.token, nid)["note"], "First note content")
        # idempotent
        nid2=self.notes.add(self.token, cid, "First note content", operation_key="n1")
        self.assertEqual(nid, nid2)
        rid=self.reqs.add(self.token, cid, "Need dashboard", operation_key="r1")
        self.assertEqual(len(self.reqs.list(self.token, cid)),1)
        self.reqs.remove(self.token, rid, operation_key="r1_rm")
        self.assertEqual(len(self.reqs.list(self.token, cid)),0)

    def test_customer_orders_fsm(self):
        cid=self.cust.create(self.token, "Carol", "carol@example.com", "", operation_key="c2")
        oid=self.orders.create(self.token, cid, "Order1", "Desc", 100000, operation_key="o1")
        self.assertEqual(self.orders.get(self.token, oid)["status"], "draft")
        self.orders.confirm(self.token, oid, operation_key="o1_conf")
        self.assertEqual(self.orders.get(self.token, oid)["status"], "confirmed")
        self.orders.fulfill(self.token, oid, operation_key="o1_fulf")
        self.assertEqual(self.orders.get(self.token, oid)["status"], "fulfilled")
        # cannot cancel after fulfilled
        with self.assertRaises(Exception):
            self.orders.cancel(self.token, oid, operation_key="o1_cancel")
        # new order cancel from draft
        oid2=self.orders.create(self.token, cid, "Order2", "", 50000, operation_key="o2")
        self.orders.cancel(self.token, oid2, operation_key="o2_cancel")
        self.assertEqual(self.orders.get(self.token, oid2)["status"], "cancelled")

    def test_project_history(self):
        cid=self.cust.create(self.token, "Dan", "dan@example.com", "", operation_key="c3")
        tid=self.pt.create(self.token, "Build feature", operation_key="t1", description="desc")
        lid=self.projects.link(self.token, cid, tid, operation_key="link1")
        lst=self.projects.list(self.token, cid)
        self.assertEqual(len(lst),1)
        self.assertEqual(lst[0]["project_task"]["title"], "Build feature")
        self.projects.unlink(self.token, lid, operation_key="link1_rm")
        self.assertEqual(len(self.projects.list(self.token, cid)),0)

    def test_communication_approval_gate_no_auto_send(self):
        cid=self.cust.create(self.token, "Eve", "eve@example.com", "", operation_key="c4")
        did=self.comm.create(self.token, cid, "email", "Hello", operation_key="comm1")
        # without approval or safe workflow, should fail
        with self.assertRaises(Exception) as cm:
            self.crm_comms.send(self.token, did, operation_key="send1")
        self.assertIn("approval", str(cm.exception).lower())
        # request approval
        aid=self.approvals.request(self.token, did, operation_key="appr1")
        self.assertEqual(self.approvals.get(self.token, aid)["effective_status"], "pending")
        # without approve, still fail
        with self.assertRaises(Exception):
            self.crm_comms.send(self.token, did, operation_key="send2", approval_id=aid)
        # approve
        self.approvals.approve(self.token, aid, operation_key="appr1_ok")
        self.assertEqual(self.approvals.get(self.token, aid)["effective_status"], "approved")
        # now send succeeds
        self.crm_comms.send(self.token, did, operation_key="send3", approval_id=aid)
        self.assertEqual(self.comm.get(self.token, did)["status"], "sent")
        # idempotent send with same op key
        self.crm_comms.send(self.token, did, operation_key="send3", approval_id=aid)
        self.assertEqual(self.comm.get(self.token, did)["status"], "sent")
        # approval cannot be reused for different communication? test mismatch
        cid2=self.cust.create(self.token, "Eve2", "eve2@example.com", "", operation_key="c4b")
        did2=self.comm.create(self.token, cid2, "email", "Hello2", operation_key="comm2")
        with self.assertRaises(Exception):
            self.crm_comms.send(self.token, did2, operation_key="send_mismatch", approval_id=aid)

    def test_approval_expiry(self):
        cid=self.cust.create(self.token, "Frank", "frank@example.com", "", operation_key="c5")
        did=self.comm.create(self.token, cid, "email", "Hi", operation_key="comm_exp")
        aid=self.approvals.request(self.token, did, operation_key="appr_exp", ttl=60)
        # fast forward past expiry
        self.clock.now += 61
        # refresh auth token? token still valid? Clock now is used for auth expiry? Auth token expiry 30min =1800, so still ok.
        # need new approvals object with updated clock (our clock already updated)
        with self.assertRaises(Exception) as cm:
            self.crm_comms.send(self.token, did, operation_key="send_exp", approval_id=aid)
        self.assertIn("expired", str(cm.exception).lower())
        # check effective status
        self.assertEqual(self.approvals.get(self.token, aid)["effective_status"], "expired")
        # reset clock
        self.clock.now = 1800000000

    def test_safe_limited_workflow_requires_explicit_config(self):
        cid=self.cust.create(self.token, "Grace", "grace@example.com", "", operation_key="c6")
        did=self.comm.create(self.token, cid, "email", "Safe msg", operation_key="comm_safe1")
        # no config -> must fail
        with self.assertRaises(Exception):
            self.crm_comms.send(self.token, did, operation_key="send_safe1")
        # configure safe workflow explicitly
        self.outbound.set(self.token, operation_key="out1", enabled=True, allowed_channels="email", daily_limit=2)
        cfg=self.outbound.get(self.token)
        self.assertEqual(cfg["enabled"],1)
        self.assertIn("email", cfg["allowed_channels"])
        # now send without approval should succeed via safe workflow
        self.crm_comms.send(self.token, did, operation_key="send_safe2")
        self.assertEqual(self.comm.get(self.token, did)["status"], "sent")
        # second safe within limit
        did2=self.comm.create(self.token, cid, "email", "Safe2", operation_key="comm_safe2")
        self.crm_comms.send(self.token, did2, operation_key="send_safe3")
        self.assertEqual(self.comm.get(self.token, did2)["status"], "sent")
        # third exceeds daily_limit 2 (we already sent 2 via safe workflow? plus maybe earlier? Actually limit counts all sent, so 2 already sent, third should exceed)
        did3=self.comm.create(self.token, cid, "email", "Safe3 exceed", operation_key="comm_safe3")
        with self.assertRaises(Exception) as cm:
            self.crm_comms.send(self.token, did3, operation_key="send_safe4")
        self.assertIn("approval", str(cm.exception).lower())
        # channel not allowed
        did4=self.comm.create(self.token, cid, "sms", "SMS not allowed", operation_key="comm_safe4")
        with self.assertRaises(Exception):
            self.crm_comms.send(self.token, did4, operation_key="send_safe5")
        # clear config -> again requires approval
        self.outbound.clear(self.token, operation_key="out_clear")
        self.assertIsNone(self.outbound.get(self.token))
        did5=self.comm.create(self.token, cid, "email", "After clear", operation_key="comm_safe5")
        with self.assertRaises(Exception):
            self.crm_comms.send(self.token, did5, operation_key="send_safe6")

    def test_never_send_automatically(self):
        # Ensure that creating a draft does NOT auto-send and no background job exists
        cid=self.cust.create(self.token, "Heidi", "heidi@example.com", "", operation_key="c7")
        did=self.comm.create(self.token, cid, "email", "No auto", operation_key="comm_noauto")
        self.assertEqual(self.comm.get(self.token, did)["status"], "draft")
        # No auto-send after time passes (within token expiry)
        self.clock.now += 600
        # Still draft unless explicit send called
        self.assertEqual(self.comm.get(self.token, did)["status"], "draft")
        self.clock.now = 1800000000

    def test_timeline_and_profile(self):
        cid=self.cust.create(self.token, "Ivan", "ivan@example.com", "referral", operation_key="c8")
        self.notes.add(self.token, cid, "Note A", operation_key="n2")
        self.reqs.add(self.token, cid, "Requirement A", operation_key="r2")
        did=self.comm.create(self.token, cid, "email", "Hello timeline", operation_key="comm_tl")
        aid=self.approvals.request(self.token, did, operation_key="appr_tl")
        self.approvals.approve(self.token, aid, operation_key="appr_tl_ok")
        self.crm_comms.send(self.token, did, operation_key="send_tl", approval_id=aid)
        oid=self.orders.create(self.token, cid, "Order TL", "", 20000, operation_key="o_tl")
        tid=self.pt.create(self.token, "Feature X", operation_key="t_tl", description="dx")
        self.projects.link(self.token, cid, tid, operation_key="link_tl")
        sid=self.svc.create(self.token, "Svc TL", "Scope", "Deliver", 10000, 1, operation_key="svc_tl")
        prop_id=self.prop.create(self.token, sid, "Prop TL", "Desc", 10000, 5, operation_key="prop_tl", customer_id=cid)
        fid=self.foll.create(self.token, cid, 1800000600, "Follow TL", operation_key="foll_tl")
        tl=self.timeline.get(self.token, cid, limit=50)
        # timeline should contain at least customer, note, requirement, communication, order, project, proposal, followup
        types = [e["type"] for e in tl]
        self.assertIn("note", types)
        self.assertIn("requirement", types)
        self.assertIn("communication", types)
        self.assertIn("order", types)
        self.assertIn("project", types)
        self.assertIn("proposal", types)
        self.assertIn("followup", types)
        prof=self.profile.get(self.token, cid)
        self.assertEqual(prof["name"], "Ivan")
        self.assertEqual(prof["source"], "referral")
        self.assertEqual(prof["_note_count"],1)
        self.assertEqual(prof["_requirement_count"],1)
        full=self.profile.full(self.token, cid)
        self.assertIn("notes", full)
        self.assertIn("timeline", full)
        self.assertEqual(len(full["notes"]),1)

    def test_csv_import_export_validation(self):
        # export empty
        csv_text=self.csv.export(self.token)
        self.assertTrue(csv_text.startswith("name,contact,source"))
        # import valid
        res=self.csv.import_csv(self.token, "name,contact,source\nAlice,alice2@example.com,web\nBob,bob2@example.com,ads\n", operation_key_prefix="csv1")
        self.assertEqual(len(res["created"]),2)
        # import invalid header
        with self.assertRaises(Exception):
            self.csv.import_csv(self.token, "bad,header\n", operation_key_prefix="csv2")
        # invalid row (name too long)
        long_name = "X"*201
        with self.assertRaises(Exception):
            self.csv.import_csv(self.token, f"name,contact,source\n{long_name},c,s\n", operation_key_prefix="csv3")
        # isolation: exported should contain Alice and Bob
        csv2=self.csv.export(self.token)
        self.assertIn("alice2@example.com", csv2)

    def test_account_isolation(self):
        cid=self.cust.create(self.token, "Judy", "judy@example.com", "", operation_key="c9")
        nid=self.notes.add(self.token, cid, "Secret note", operation_key="n3")
        # other owner
        auth2=AuthService(AuthStorage(self.auth_path), self.clock)
        auth2.register("bob","other password safe")
        token2=auth2.login("bob","other password safe")
        # bob cannot see alice's customer
        with self.assertRaises(Exception):
            self.cust.get(token2, cid)
        with self.assertRaises(Exception):
            self.notes.list(token2, cid)
        with self.assertRaises(Exception):
            self.notes.get(token2, nid)
        # bob can create own customer same op key (isolated)
        cid2=Customers(AuthService(AuthStorage(self.auth_path), self.clock), StateStorage(self.state_path)).create(token2, "BobCust", "bob@example.com", "", operation_key="c9")
        self.assertNotEqual(cid, cid2)
        # bob cannot send alice's communication
        did=self.comm.create(self.token, cid, "email", "Hi", operation_key="comm_iso")
        with self.assertRaises(Exception):
            self.crm_comms.send(token2, did, operation_key="send_iso", approval_id=None)

    def test_migrations_and_integrity(self):
        self.assertEqual(LATEST_SCHEMA_VERSION, 28)
        with StateStorage(self.state_path).transaction() as conn:
            self.assertEqual(_validate_schema(conn), LATEST_SCHEMA_VERSION)
            _check_integrity(conn)
        self.assertEqual(len(list(self.priv.glob("before-v*.sqlite3"))), LATEST_SCHEMA_VERSION-1)

    def test_order_isolation_and_idempotency(self):
        cid=self.cust.create(self.token, "Karl", "karl@example.com", "", operation_key="c10")
        oid=self.orders.create(self.token, cid, "OrderDup", "", 50000, operation_key="o_dup")
        oid2=self.orders.create(self.token, cid, "OrderDup", "", 50000, operation_key="o_dup")
        self.assertEqual(oid, oid2)
        with self.assertRaises(Exception):
            self.orders.create(self.token, cid, "Other Title", "", 50000, operation_key="o_dup")

if __name__=="__main__":
    unittest.main()

