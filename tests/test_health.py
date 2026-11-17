"""Tests for read-only health checks (config, private DBs, version, no secrets)."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.health import check, check_with_backups
from ai_earn.health_cli import run as health_run
from ai_earn.state_storage import StateStorage, LATEST_SCHEMA_VERSION


class Clock:
    now = 1_800_000_000
    def __call__(self):
        return self.now


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires supported private filesystem")
class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "private"
        if os.name == "nt":
            from ai_earn.windows_security import create_private_directory
            create_private_directory(self.directory)
        else:
            self.directory.mkdir(mode=0o700)
        patcher = patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.auth_path = self.directory / "auth.sqlite3"
        self.state_path = self.directory / "state.sqlite3"
        self.environ = {
            "AI_EARN_ENV": "development",
            "AI_EARN_LOG_LEVEL": "INFO",
            "AI_EARN_AUTH_DB": str(self.auth_path),
            "AI_EARN_STATE_DB": str(self.state_path),
        }

    def test_missing_dbs_not_ok(self):
        payload = check(self.environ)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["checks"]["auth"]["status"], "missing")
        self.assertEqual(payload["checks"]["state"]["status"], "missing")
        self.assertFalse(payload["production_ready"])

    def test_invalid_env_config(self):
        env = dict(self.environ, AI_EARN_ENV="production")
        # production is actually valid for load_settings, but health still checks; use truly invalid
        env["AI_EARN_ENV"] = "invalid"
        payload = check(env)
        self.assertEqual(payload["checks"]["config"]["status"], "invalid_configuration")
        self.assertFalse(payload["ok"])

    def test_ok_after_init(self):
        auth = AuthService(AuthStorage(self.auth_path, create=True), self.clock)
        auth.register("alice", "disposable test password")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION+1):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        payload = check(self.environ)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["checks"]["auth"]["status"], "ok")
        self.assertEqual(payload["checks"]["state"]["status"], "ok")
        self.assertEqual(payload["checks"]["state"]["schema_version"], LATEST_SCHEMA_VERSION)
        self.assertEqual(payload["checks"]["version"]["latest"], LATEST_SCHEMA_VERSION)
        self.assertIn(LATEST_SCHEMA_VERSION, payload["checks"]["version"]["supported"])

    def test_with_backups_counts(self):
        auth = AuthService(AuthStorage(self.auth_path, create=True), self.clock)
        auth.register("alice", "disposable test password")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION+1):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        payload = check_with_backups(self.environ)
        self.assertTrue(payload["ok"])
        self.assertIn("backups", payload["checks"])
        self.assertEqual(payload["checks"]["backups"]["count"], LATEST_SCHEMA_VERSION-1)
        self.assertEqual(payload["checks"]["backups"]["expected"], LATEST_SCHEMA_VERSION-1)
        self.assertEqual(payload["checks"]["backups"]["status"], "ok")

    def test_cli_json_ok(self):
        auth = AuthService(AuthStorage(self.auth_path, create=True), self.clock)
        auth.register("alice", "disposable test password")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION+1):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        out, err = io.StringIO(), io.StringIO()
        rc = health_run(["--json"], self.environ, out, err)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["production_ready"])

    def test_cli_not_ok_exit_1_missing(self):
        out, err = io.StringIO(), io.StringIO()
        rc = health_run([], self.environ, out, err)
        self.assertEqual(rc, 1)
        self.assertIn("ok: False", out.getvalue())

    def test_cli_with_backups_json(self):
        auth = AuthService(AuthStorage(self.auth_path, create=True), self.clock)
        auth.register("alice", "disposable test password")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION+1):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        out, err = io.StringIO(), io.StringIO()
        rc = health_run(["--json", "--with-backups"], self.environ, out, err)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("backups", payload["checks"])

    def test_no_secrets_in_payload(self):
        payload = check(self.environ)
        blob = json.dumps(payload)
        # No raw path value should leak as secret? Paths are not secrets, but ensure no password
        self.assertNotIn("disposable test password", blob)
        self.assertNotIn("token", blob.lower())

    def test_production_ready_always_false(self):
        # Even with valid DBs, health never flips production_ready
        auth = AuthService(AuthStorage(self.auth_path, create=True), self.clock)
        auth.register("alice", "disposable test password")
        state = StateStorage(self.state_path, create=True, confirmed=True)
        for v in range(2, LATEST_SCHEMA_VERSION+1):
            getattr(state, f"migrate_to_v{v}")(self.directory / f"before-v{v}.sqlite3", confirmed=True)
        payload = check(dict(self.environ, AI_EARN_ENV="production"))
        self.assertFalse(payload["production_ready"])


if __name__ == "__main__":
    unittest.main()
