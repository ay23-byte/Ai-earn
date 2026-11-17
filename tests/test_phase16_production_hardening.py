"""Phase 16 — production hardening and deployment checks."""
import json
import pathlib
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class ProductionHardeningTests(unittest.TestCase):
    def test_production_config_exists_and_validates(self):
        p = ROOT / "ai_earn" / "production_config.py"
        self.assertTrue(p.exists(), "production_config.py missing")
        text = p.read_text()
        self.assertIn("validate_production", text)
        self.assertIn("no_network_db_exposure", text)
        self.assertIn("Never expose", text)
        self.assertIn("production_ready", text)
        from ai_earn.production_config import validate_production, production_summary
        env = {"AI_EARN_ENV": "production", "AI_EARN_AUTH_DB": "/tmp/a/auth.sqlite3", "AI_EARN_STATE_DB": "/tmp/a/state.sqlite3"}
        checks = validate_production(env)
        self.assertTrue(any(c.id == "no_network_db_exposure" for c in checks))
        summary = production_summary(env)
        self.assertFalse(summary["production_ready"])
        self.assertIn("127.0.0.1", summary["guidance"])

    def test_secret_management(self):
        p = ROOT / "ai_earn" / "secrets.py"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("generate_secret", text)
        self.assertIn("redact", text)
        self.assertIn("validate_secret", text)
        self.assertIn("[REDACTED", text)
        from ai_earn.secrets import redact, validate_secret, generate_secret
        s = generate_secret(32)
        self.assertTrue(32 <= len(s) <= 256)
        self.assertIn("[REDACTED", redact(s))
        with self.assertRaises(ValueError):
            validate_secret("x", "short")
        from ai_earn.secrets import scan_for_forbidden_text
        self.assertEqual(scan_for_forbidden_text("hello world"), [])
        self.assertTrue(scan_for_forbidden_text("BEGIN" + " PRIVATE KEY"), [])

    def test_database_backups(self):
        p = ROOT / "ai_earn" / "backup.py"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("create_backup", text)
        self.assertIn("verified", text.lower())
        self.assertIn("never overwrite", text.lower())
        self.assertNotIn("0.0.0.0", text)

    def test_recovery_procedures_docs(self):
        for doc in ["docs/BACKUP_AND_RECOVERY.md", "docs/ROLLBACK.md", "docs/MIGRATION.md"]:
            self.assertTrue((ROOT / doc).exists(), f"missing {doc}")
            self.assertIn("backup", (ROOT / doc).read_text().lower())
        rollback = (ROOT / "docs/ROLLBACK.md").read_text()
        self.assertIn("new private path", rollback.lower())
        self.assertIn("never overwrite", rollback.lower())

    def test_health_monitoring(self):
        from ai_earn.health import check, check_with_backups
        from ai_earn.monitoring import Monitor
        env = {"AI_EARN_ENV": "development", "AI_EARN_AUTH_DB": "", "AI_EARN_STATE_DB": ""}
        payload = check(env)
        self.assertIn("ok", payload)
        self.assertFalse(payload["production_ready"])
        m = Monitor(environ=env)
        sample = m.poll()
        self.assertIn("ok", sample.payload)

    def test_structured_logs(self):
        p = ROOT / "ai_earn" / "structured_logging.py"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("StructuredLogger", text)
        self.assertIn("redact", text.lower())
        self.assertIn("allowlist", text.lower())
        from ai_earn.structured_logging import StructuredLogger
        import io
        buf = io.StringIO()
        logger = StructuredLogger(stream=buf, minimum_level="INFO")
        logger.info("health_check", message="test", fields={"password": "secret12345678901234567890", "ok": True})
        out = buf.getvalue()
        self.assertIn("health_check", out)
        self.assertNotIn("secret123", out)
        self.assertIn("[REDACTED", out)

    def test_error_reporting(self):
        p = ROOT / "ai_earn" / "error_reporting.py"
        self.assertTrue(p.exists())
        from ai_earn.error_reporting import report_error
        import io
        from ai_earn.structured_logging import StructuredLogger
        buf = io.StringIO()
        logger = StructuredLogger(stream=buf, minimum_level="INFO")
        err = report_error(ValueError("bad input"), fields={"path": "/secret/db.sqlite3"}, logger=logger)
        self.assertTrue(len(err.error_id) == 8)
        self.assertEqual(err.code, "invalid_input")
        self.assertNotIn("/secret", buf.getvalue())
        self.assertIn(err.error_id, buf.getvalue())

    def test_dependency_security_checks(self):
        p = ROOT / "tools" / "security_check.py"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("pip-audit", text.lower())
        # Run scan — should pass (split pattern avoids self-trigger)
        import subprocess, sys
        result = subprocess.run([sys.executable, "-m", "tools.security_check"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # audit mode also passes on stdlib
        result2 = subprocess.run([sys.executable, "-m", "tools.security_check", "--audit"], capture_output=True, text=True, timeout=10)
        self.assertEqual(result2.returncode, 0, result2.stdout + result2.stderr)

    def test_ci_workflow(self):
        p = ROOT / ".github" / "workflows" / "tests.yml"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("compileall", text)
        self.assertIn("unittest", text)
        self.assertIn("security_check", text.lower())
        self.assertIn("pip-audit", text.lower())
        self.assertIn("health", text.lower())
        self.assertIn("doctor", text.lower())
        self.assertIn("0.0.0.0", text)

    def test_deployment_documentation(self):
        p = ROOT / "docs" / "DEPLOYMENT.md"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("Windows", text)
        self.assertIn("127.0.0.1", text)
        self.assertIn("Never expose", text)
        self.assertIn("sqlite", text.lower())
        self.assertIn("production", text.lower())
        self.assertIn("architecture", text.lower())
        # Every 0.0.0.0 must be in a warning/never/not context
        for m in re.finditer(r"0\.0\.0\.0", text):
            window = text[max(0, m.start()-120):m.end()+40].lower()
            self.assertTrue(any(w in window for w in ["never", "not", "not binding"]), f"0.0.0.0 without never/not warning at {m.start()}: {window!r}")

    def test_database_migration_procedure(self):
        p = ROOT / "docs" / "MIGRATION.md"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("migrate_to_v", text)
        self.assertIn("backup", text.lower())
        self.assertIn("never overwrite", text.lower())

    def test_rollback_documentation(self):
        p = ROOT / "docs" / "ROLLBACK.md"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("rollback", text.lower())
        self.assertIn("new private path", text.lower())

    def test_user_data_export(self):
        p = ROOT / "ai_earn" / "data_export.py"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("export_user_data", text)
        self.assertIn("owner-scoped", text.lower())
        self.assertIn("redacted", text.lower())
        import tempfile
        from unittest.mock import patch
        from ai_earn.auth_storage import AuthStorage
        from ai_earn.auth import AuthService
        from ai_earn.state_storage import StateStorage
        from ai_earn.opportunities import Opportunities
        tmp = tempfile.mkdtemp()
        private = Path(tmp) / "private"
        private.mkdir(mode=0o700)
        auth_db = private / "auth.sqlite3"
        state_db = private / "state.sqlite3"
        with patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000):
            auth_store = AuthStorage(auth_db)
            auth = AuthService(auth_store)
            auth.register("alice", "StrongPass123!")
            token = auth.login("alice", "StrongPass123!")
        state = StateStorage(state_db, create=True, confirmed=True)
        for v in range(2, 29):
            m = getattr(state, f"migrate_to_v{v}", None)
            if m:
                m(private / f"before-v{v}.sqlite3", confirmed=True)
        opp = Opportunities(auth, state)
        opp.create(token, "Test Opp", "https://example.com/a", 50000, operation_key="opp1")
        from ai_earn.data_export import export_user_data
        result = export_user_data(token, auth_db=auth_db, state_db=state_db, destination=private / "export.json")
        self.assertIn("owner_id", result)
        self.assertIn("tables", result)
        self.assertGreater(result["total_rows"], 0)
        j = json.dumps(result)
        self.assertNotIn("password_hash", j)
        self.assertNotIn("recovery_hash", j)

    def test_account_deletion_safeguards(self):
        p = ROOT / "ai_earn" / "account_deletion.py"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("DELETE", text)
        self.assertIn("backup", text.lower())
        self.assertIn("password", text.lower())
        self.assertIn("confirmation", text.lower())
        self.assertIn("owner_id", text.lower())
        import tempfile
        from unittest.mock import patch
        from ai_earn.auth_storage import AuthStorage
        from ai_earn.auth import AuthService
        from ai_earn.state_storage import StateStorage
        from ai_earn.opportunities import Opportunities
        from ai_earn.account_deletion import prepare_deletion_backup, confirm_deletion
        tmp = tempfile.mkdtemp()
        private = Path(tmp) / "private"
        private.mkdir(mode=0o700)
        auth_db = private / "auth.sqlite3"
        state_db = private / "state.sqlite3"
        with patch("ai_earn.auth.PASSWORD_ITERATIONS", 1000):
            auth_store = AuthStorage(auth_db)
            auth = AuthService(auth_store)
            auth.register("bob", "StrongPass123!")
            token = auth.login("bob", "StrongPass123!")
        state = StateStorage(state_db, create=True, confirmed=True)
        for v in range(2, 29):
            m = getattr(state, f"migrate_to_v{v}", None)
            if m:
                m(private / f"before-v{v}.sqlite3", confirmed=True)
        opp = Opportunities(auth, state)
        opp.create(token, "Bob Opp", "https://example.com/b", 10000, operation_key="opp2")
        backup = private / "before-delete.sqlite3"
        prep = prepare_deletion_backup(token, auth_db=auth_db, state_db=state_db, backup_path=backup)
        self.assertIn("confirmation_phrase", prep)
        self.assertTrue(backup.exists())
        phrase = prep["confirmation_phrase"]
        with self.assertRaises(ValueError):
            confirm_deletion(token, "StrongPass123!", "DELETE wrong", auth_db=auth_db, state_db=state_db, backup_path=backup)
        result = confirm_deletion(token, "StrongPass123!", phrase, auth_db=auth_db, state_db=state_db, backup_path=backup)
        self.assertGreater(result["total_deleted_rows"], 0)
        from ai_earn.data_export import export_user_data
        with self.assertRaises(Exception):
            export_user_data(token, auth_db=auth_db, state_db=state_db)

    def test_privacy_documentation(self):
        p = ROOT / "docs" / "PRIVACY.md"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("Privacy", text)
        self.assertIn("export", text.lower())
        self.assertIn("deletion", text.lower())
        self.assertIn("retention", text.lower())
        self.assertIn("owner-scoped", text.lower())

    def test_operational_runbooks(self):
        p = ROOT / "docs" / "OPERATIONAL_RUNBOOKS.md"
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("Runbook", text)
        self.assertIn("Health check", text)
        self.assertIn("Backup", text)
        self.assertIn("Restore", text)
        self.assertIn("Windows", text)

    def test_never_expose_sqlite_over_network(self):
        dep = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
        self.assertIn("--bind 127.0.0.1", dep)
        for m in re.finditer(r"0\.0\.0\.0", dep):
            window = dep[max(0, m.start()-120):m.end()+40].lower()
            self.assertTrue(any(w in window for w in ["never", "not"]), f"0.0.0.0 without never/not at {m.start()}: {window!r}")
        # Check dashboard: must not bind 0.0.0.0 and must warn if mentioning
        for rel in ["dashboard"]:
            for path in (ROOT / rel).rglob("*"):
                if path.suffix in (".py", ".js", ".html"):
                    text = path.read_text(errors="ignore")
                    if "0.0.0.0" in text:
                        self.assertIn("never", text.lower(), f"{path} mentions 0.0.0.0 without never warning")

if __name__ == "__main__":
    unittest.main()
