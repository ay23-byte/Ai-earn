import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_earn.__main__ import main
from ai_earn.responses import failure, success


class ResponseTests(unittest.TestCase):
    def test_envelope(self):
        self.assertEqual(success({"x": 1}), {"ok": True, "data": {"x": 1}, "error": None})
        self.assertEqual(failure("bad", "message"), {
            "ok": False, "data": None, "error": {"code": "bad", "message": "message"},
        })


class CliTests(unittest.TestCase):
    def run_main(self, args, env):
        out, err = io.StringIO(), io.StringIO()
        exit_code = main(args, env, out, err)
        return exit_code, json.loads(out.getvalue()), err.getvalue()

    def test_status_is_honest_and_isolated_from_secret_env(self):
        code, response, logs = self.run_main(["status"], {"AI_EARN_ENV": "test", "PRIVATE_KEY": "secret-marker"})
        self.assertEqual(code, 0)
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["environment"], "test")
        self.assertFalse(response["data"]["production_ready"])
        self.assertEqual(response["data"]["capabilities"]["authentication"], os.name in {"posix", "nt"})
        self.assertEqual(response["data"]["capabilities"]["persistent_storage"], os.name in {"posix", "nt"})
        self.assertFalse(response["data"]["capabilities"]["network_listener"])
        self.assertFalse(response["data"]["capabilities"]["payments"])
        self.assertFalse(response["data"]["capabilities"]["earning_workflows"])
        self.assertNotIn("secret-marker", json.dumps(response) + logs)
        self.assertEqual(json.loads(logs)["event"], "status_requested")

    def test_production_status_does_not_claim_auth_available(self):
        code, response, _ = self.run_main(["status"], {"AI_EARN_ENV": "production"})
        self.assertEqual(code, 0)
        self.assertFalse(response["data"]["capabilities"]["authentication"])
        self.assertFalse(response["data"]["capabilities"]["persistent_storage"])
        self.assertFalse(response["data"]["production_ready"])

    def test_unsupported_platform_status_does_not_claim_auth_available(self):
        with patch("ai_earn.status.os.name", "unsupported"):
            code, response, _ = self.run_main(["status"], {})
        self.assertEqual(code, 0)
        self.assertFalse(response["data"]["capabilities"]["authentication"])
        self.assertFalse(response["data"]["capabilities"]["persistent_storage"])

    def test_windows_status_reports_development_but_not_production(self):
        with patch("ai_earn.status.os.name", "nt"):
            _, development, _ = self.run_main(["status"], {})
            _, production, _ = self.run_main(["status"], {"AI_EARN_ENV": "production"})
        self.assertTrue(development["data"]["capabilities"]["authentication"])
        self.assertTrue(development["data"]["capabilities"]["persistent_storage"])
        self.assertFalse(production["data"]["capabilities"]["authentication"])
        self.assertFalse(production["data"]["production_ready"])

    def test_windows_status_respects_disabled_gate(self):
        with patch("ai_earn.status.os.name", "nt"), patch("ai_earn.status._windows_auth_verified", return_value=False):
            _, response, _ = self.run_main(["status"], {})
        self.assertFalse(response["data"]["capabilities"]["authentication"])
        self.assertFalse(response["data"]["capabilities"]["persistent_storage"])

    def test_invalid_command_does_not_echo_arguments(self):
        code, response, logs = self.run_main(["secret-argument"], {})
        self.assertEqual(code, 2)
        self.assertEqual(response["error"]["code"], "invalid_command")
        self.assertNotIn("secret-argument", json.dumps(response) + logs)

    def test_bad_configuration_is_not_echoed(self):
        for key in ("AI_EARN_ENV", "AI_EARN_LOG_LEVEL"):
            with self.subTest(key=key):
                code, response, logs = self.run_main(["status"], {key: "secret-configuration"})
                self.assertEqual(code, 2)
                self.assertEqual(response["error"]["code"], "invalid_configuration")
                self.assertIn(key, response["error"]["message"])
                self.assertNotIn("secret-configuration", json.dumps(response) + logs)

    def test_unexpected_exception_does_not_leak(self):
        with patch("ai_earn.__main__.current_status", side_effect=RuntimeError("secret exception")):
            code, response, logs = self.run_main(["status"], {})
        self.assertEqual(code, 1)
        self.assertEqual(response["error"]["code"], "internal_error")
        self.assertEqual(json.loads(logs)["event"], "internal_error")
        self.assertNotIn("secret exception", json.dumps(response) + logs)

    def test_quiet_log_level(self):
        code, _, logs = self.run_main(["status"], {"AI_EARN_LOG_LEVEL": "WARNING"})
        self.assertEqual(code, 0)
        self.assertEqual(logs, "")

    def test_clean_copy_runs_without_install_or_network(self):
        source = Path(__file__).resolve().parents[1] / "ai_earn"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            shutil.copytree(source, Path(directory) / "ai_earn", ignore=shutil.ignore_patterns("__pycache__"))
            environment = os.environ.copy()
            environment.pop("AI_EARN_ENV", None)
            environment.pop("AI_EARN_LOG_LEVEL", None)
            environment.pop("PYTHONPATH", None)
            result = subprocess.run([sys.executable, "-m", "ai_earn", "status"], cwd=directory,
                                    env=environment, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(json.loads(result.stderr)["event"], "status_requested")
