"""Deployment login checks use synthetic credentials and a temporary database."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agendex.auth import AuthConfig, COOKIE_NAME, SESSION_SECONDS, hash_password
from test_support import FakeJev, create_fixture, isolated_app_factory, policy_body


create_app = isolated_app_factory()


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password("fixture-password")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agendex-auth-")
        self.path = Path(self.temp.name) / "auth.sqlite3"
        create_fixture(self.path, count=2)
        self.fake = FakeJev()
        environment = {
            "AGENDEX_AUTH_ENABLED": "1", "AGENDEX_AUTH_USERNAME": "fixture-user",
            "AGENDEX_AUTH_PASSWORD_HASH": self.password_hash,
            "AGENDEX_SESSION_SECRET": "fixture-secret-with-at-least-32-characters",
            "AGENDEX_PUBLIC_ORIGIN": "https://agendex.icu",
        }
        with patch.dict(os.environ, environment):
            self.app = create_app(self.path, client=self.fake, start_workers=False)
        self.client = TestClient(self.app, base_url="https://agendex.icu")
        self.client.__enter__()
        self.origin = {"Origin": "https://agendex.icu"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self, password="fixture-password", **options):
        return self.client.post("/api/auth/login", json={"username": "fixture-user", "password": password},
                                headers=self.origin, **options)

    def test_anonymous_api_exports_and_documentation_are_protected(self):
        for path in ["/api/health", "/api/repositories", "/api/repositories/export.csv", "/api/jobs", "/docs", "/openapi.json"]:
            result = self.client.get(path)
            self.assertEqual(result.status_code, 401, path)
            self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/api/auth/session").json(), {"enabled": True, "authenticated": False, "username": None})
        self.assertEqual(self.fake.requests, [])

    def test_login_session_cookie_and_logout(self):
        wrong = self.login("wrong-password")
        self.assertEqual((wrong.status_code, wrong.json()["detail"]), (401, "账号或密码不正确"))
        result = self.login()
        self.assertEqual(result.json(), {"enabled": True, "authenticated": True, "username": "fixture-user"})
        cookie = result.headers["set-cookie"]
        for flag in ["HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=43200"]:
            self.assertIn(flag, cookie)
        self.assertNotIn("fixture-password", cookie)
        self.assertEqual(self.client.get("/api/stats").status_code, 200)
        self.assertTrue(self.client.get("/api/auth/session").json()["authenticated"])
        logout = self.client.post("/api/auth/logout", headers=self.origin)
        self.assertFalse(logout.json()["authenticated"])
        self.assertEqual(self.client.get("/api/stats").status_code, 401)

    def test_writes_require_trusted_origin_and_host_even_with_session(self):
        self.login()
        for headers in [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}, {**self.origin, "Host": "evil.example"}]:
            result = self.client.post("/api/policies", json=policy_body(), headers=headers)
            self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.get("/api/stats", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/policies", json=policy_body(), headers=self.origin).status_code, 201)
        self.assertEqual(self.fake.requests, [])

    def test_tampered_and_expired_sessions_are_rejected(self):
        self.login()
        token = self.client.cookies.get(COOKIE_NAME)
        manager = self.app.state.auth
        self.assertFalse(manager.session(token + "tampered")["authenticated"])
        with patch("agendex.auth.time.time", return_value=__import__("time").time() + SESSION_SECONDS + 1):
            self.assertFalse(manager.session(token)["authenticated"])
        for malformed in ["", "...", "not-a-cookie", "%%%%.invalid"]:
            self.assertFalse(manager.session(malformed)["authenticated"])

    def test_repeated_failed_logins_are_limited(self):
        for _ in range(5):
            self.assertEqual(self.login("wrong-password").status_code, 401)
        self.assertEqual(self.login("wrong-password").status_code, 429)

    def test_missing_deployment_secrets_fail_closed(self):
        with patch.dict(os.environ, {"AGENDEX_AUTH_ENABLED": "1", "AGENDEX_SESSION_SECRET": "short"}):
            with self.assertRaises(ValueError):
                AuthConfig.from_environment()
        with patch.dict(os.environ, {"AGENDEX_AUTH_ENABLED": "0", "AGENDEX_PUBLIC_ORIGIN": "https://agendex.icu"}):
            with self.assertRaises(ValueError):
                AuthConfig.from_environment()

    def test_local_mode_preserves_existing_workflow(self):
        with patch.dict(os.environ, {"AGENDEX_AUTH_ENABLED": "0", "AGENDEX_PUBLIC_ORIGIN": ""}):
            app = create_app(self.path, client=self.fake, start_workers=False)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/auth/session").json(), {"enabled": False, "authenticated": True, "username": None})
            self.assertEqual(client.get("/api/repositories").status_code, 200)
            self.assertEqual(client.post("/api/policies", json=policy_body()).status_code, 201)
