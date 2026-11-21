"""First-run onboarding checklist — computed from real owner data only."""

import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_earn.auth import AuthService, AccessDenied
from ai_earn.auth_storage import AuthStorage
from ai_earn.onboarding import OnboardingChecklist
from ai_earn.services import Services
from ai_earn.state_storage import StateStorage


def _k(p="onb"):
    return f"{p}_{secrets.token_hex(16)}"


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires private filesystem")
class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(d)
        else:
            d.mkdir(mode=0o700)
        self.auth = AuthService(AuthStorage(d / "auth.sqlite3"))
        state = StateStorage(d / "state.sqlite3", create=True, confirmed=True)
        for v in range(2, 31):
            getattr(state, f"migrate_to_v{v}")(d / f"before-v{v}.sqlite3", confirmed=True)
        self.state = StateStorage(d / "state.sqlite3")
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth.register("alice", "StrongPass12345678")
        self.alice = self.auth.login("alice", "StrongPass12345678")
        self.auth.register("bob", "StrongPass12345678")
        self.bob = self.auth.login("bob", "StrongPass12345678")

    def test_new_owner_has_nothing_done_and_a_next_action(self):
        result = OnboardingChecklist(self.auth, self.state).get(self.alice)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["total"], len(result["items"]))
        self.assertIn("service", result["next_action"].lower())
        self.assertTrue(result["read_only"])

    def test_progress_reflects_actual_records_and_is_owner_scoped(self):
        svc = Services(self.auth, self.state)
        sid = svc.create(self.alice, "Web Dev", scope="s", deliverable="d", price_paise=1000, revisions=1, operation_key=_k("svc"))
        svc.publish(self.alice, sid, operation_key=_k("pub"))
        result = OnboardingChecklist(self.auth, self.state).get(self.alice)
        done = {i["key"] for i in result["items"] if i["done"]}
        self.assertEqual(done, {"service_created", "service_published"})
        bob_result = OnboardingChecklist(self.auth, self.state).get(self.bob)
        self.assertEqual(bob_result["completed"], 0)

    def test_bad_token_denied(self):
        with self.assertRaises(AccessDenied):
            OnboardingChecklist(self.auth, self.state).get("bad-token")


if __name__ == "__main__":
    unittest.main()
