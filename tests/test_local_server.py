"""Integration tests for the loopback HTTP adapter and browser session boundary."""
import http.client
import json
from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
import threading
import unittest

from ai_earn.auth import AccessDenied, AuthError
from ai_earn.local_server import EarnOSHTTPServer, EarnOSHandler


class FakeAuth:
    def __init__(self):
        self.signed_out = False

    def login(self, username, password):
        if username != "alice" or password != "correct horse battery":
            raise AuthError("Invalid credentials.")
        self.signed_out = False
        return "opaque-service-session"

    def profile(self, token):
        if token != "opaque-service-session" or self.signed_out:
            raise AccessDenied("Access denied.")
        return {"id": "account-a", "username": "alice", "display_name": "Alice"}

    def logout(self, token):
        self.signed_out = True


class FakeMetrics:
    def get(self, token):
        assert token == "opaque-service-session"
        return {"read_only": True, "schema_version": 29, "work": {"opportunity_drafts": 2}}


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "index.html").write_text("Earn OS test shell", encoding="utf-8")
        self.auth = FakeAuth()
        self.server = EarnOSHTTPServer(("127.0.0.1", 0), EarnOSHandler, self.auth, object(), FakeMetrics(), root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.connection.close()

    def request(self, method, path, body=None, headers=None):
        self.connection.request(method, path, body=body, headers=headers or {})
        response = self.connection.getresponse()
        payload = response.read()
        return response.status, response.getheaders(), json.loads(payload) if payload else {}

    @staticmethod
    def cookie(headers, name):
        jar = SimpleCookie()
        for key, value in headers:
            if key.lower() == "set-cookie":
                jar.load(value)
        return jar[name].value if name in jar else ""

    def test_login_cookie_metrics_and_logout(self):
        status, headers, _ = self.request("GET", "/api/v1/csrf", headers={"Host": self.host})
        self.assertEqual(status, 200)
        csrf_cookie = self.cookie(headers, "ai_earn_csrf")
        self.assertTrue(csrf_cookie)
        common = {"Host": self.host, "Origin": "http://" + self.host, "Cookie": "ai_earn_csrf=" + csrf_cookie, "X-CSRF-Token": csrf_cookie, "Content-Type": "application/json"}
        status, headers, data = self.request("POST", "/api/v1/auth/login", json.dumps({"username": "alice", "password": "correct horse battery"}), common)
        self.assertEqual(status, 200)
        self.assertNotIn("opaque-service-session", json.dumps(data))
        session = self.cookie(headers, "ai_earn_session")
        self.assertTrue(session)
        session_headers = {"Host": self.host, "Cookie": "ai_earn_session=" + session}
        status, _, metrics = self.request("GET", "/api/v1/dashboard/metrics", headers=session_headers)
        self.assertEqual(status, 200)
        self.assertEqual(metrics["data"]["metrics"]["work"]["opportunity_drafts"], 2)
        status, _, me = self.request("GET", "/api/v1/me", headers=session_headers)
        self.assertEqual(status, 200)
        self.assertEqual(me["data"]["account"]["username"], "alice")
        logout_headers = dict(common)
        logout_headers["Cookie"] = "ai_earn_session=" + session + "; ai_earn_csrf=" + csrf_cookie
        status, _, _ = self.request("POST", "/api/v1/auth/logout", "{}", logout_headers)
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/v1/me", headers=session_headers)
        self.assertEqual(status, 401)

    def test_rejects_missing_csrf_and_cross_origin(self):
        body = json.dumps({"username": "alice", "password": "correct horse battery"})
        status, _, _ = self.request("POST", "/api/v1/auth/login", body, {"Host": self.host, "Origin": "http://" + self.host, "Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/v1/auth/login", body, {"Host": self.host, "Origin": "https://attacker.example", "Cookie": "ai_earn_csrf=x", "X-CSRF-Token": "x", "Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_static_shell_and_unknown_api_route(self):
        status, _, _ = self.request("GET", "/", headers={"Host": self.host})
        self.assertEqual(status, 200)
        status, _, payload = self.request("GET", "/api/v1/not-a-route", headers={"Host": self.host})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
