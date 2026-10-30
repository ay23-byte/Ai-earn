"""Disposable owner-scoped state-audit reads."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AccessDenied, AuthService, SESSION_SECONDS
from ai_earn.auth_storage import AuthStorage
from ai_earn.order_drafts import OrderDrafts
from ai_earn.state_audit import AuditQueryError, StateAudit
from ai_earn.state_storage import StateStorage


class Clock:
    now = 1_800_000_000

    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported private filesystem")
class StateAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        password_patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        password_patcher.start()
        self.addCleanup(password_patcher.stop)
        self.clock = Clock()
        self.auth = AuthService(AuthStorage(self.directory / "auth.sqlite3"), self.clock)
        for name in ("alice", "bobby"):
            self.auth.register(name, "disposable test password")
        self.alice = self.auth.login("alice", "disposable test password")
        self.bob = self.auth.login("bobby", "disposable test password")
        self.state = StateStorage(self.directory / "state.sqlite3", create=True, confirmed=True)
        for version in range(2, 7):
            getattr(self.state, f"migrate_to_v{version}")(self.directory / f"v{version-1}.sqlite3", confirmed=True)
        self.orders = OrderDrafts(self.auth, self.state)
        self.audit = StateAudit(self.auth, self.state)

    def test_owner_isolation_and_pagination(self):
        first = self.orders.create(self.alice, "First", 1, operation_key="a")
        other = self.orders.create(self.bob, "Private other", 2, operation_key="b")
        second = self.orders.create(self.alice, "Second", 3, operation_key="c")
        first_page = self.audit.list_events(self.alice, limit=1)
        next_page = self.audit.list_events(self.alice, after_id=first_page[0]["id"], limit=1)
        self.assertEqual([x["resource_id"] for x in first_page + next_page], [first, second])
        self.assertEqual(self.audit.list_events(self.alice, after_id=next_page[0]["id"]), [])
        self.assertEqual([x["resource_id"] for x in self.audit.list_events(self.bob)], [other])
        self.assertEqual(set(first_page[0]), {"id", "occurred_at", "event", "resource_type", "resource_id"})
        self.assertNotIn(self.alice, str(first_page))
        self.assertNotIn("First", str(first_page))
        self.assertNotIn("Private other", str(first_page))

    def test_empty_and_strict_page_bounds(self):
        self.assertEqual(self.audit.list_events(self.alice), [])
        for cursor in (-1, True, 1.0, None, 9_223_372_036_854_775_808):
            with self.assertRaises(AuditQueryError):
                self.audit.list_events(self.alice, after_id=cursor)
        for size in (0, 101, True, 1.0, None):
            with self.assertRaises(AuditQueryError):
                self.audit.list_events(self.alice, limit=size)
        self.assertEqual(self.audit.list_events(self.alice, after_id=9_223_372_036_854_775_807, limit=100), [])

    def test_revoked_expired_and_invalid_sessions_denied(self):
        self.auth.logout(self.bob)
        for token in (self.bob, "", "x" * 43):
            with self.assertRaises(AccessDenied):
                self.audit.list_events(token)
        self.clock.now += SESSION_SECONDS
        with self.assertRaises(AccessDenied):
            self.audit.list_events(self.alice)

    def test_read_has_no_audit_or_operation_side_effects(self):
        self.orders.create(self.alice, "Scope", 1, operation_key="key")
        before = self.audit.list_events(self.alice)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pages = list(pool.map(lambda _: self.audit.list_events(self.alice), range(2)))
        self.assertEqual(pages, [before, before])
        with self.state.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM state_audit").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM state_operations").fetchone()[0], 1)

    def test_backup_and_reopen_preserve_events(self):
        self.orders.create(self.alice, "Scope", 1, operation_key="key")
        expected = self.audit.list_events(self.alice)
        backup = self.directory / "audit-copy.sqlite3"
        self.state.backup_to(backup, confirmed=True)
        restored = StateAudit(self.auth, StateStorage(backup))
        self.assertEqual(restored.list_events(self.alice), expected)
