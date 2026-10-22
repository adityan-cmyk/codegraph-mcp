"""Per-user key store + admin user-key endpoints.

Covers: create → resolve → revoke cycle, hash-only persistence, fail-closed
resolution (unknown/revoked/malformed), admin-token enforcement on the CRUD
endpoints, and input validation.
"""

import unittest
from unittest.mock import patch


class ClientTokenStoreTestCase(unittest.TestCase):
    def setUp(self):
        import app.core.client_tokens as ct

        ct._memory_repo = None
        ct._neg_cache.clear()
        self.ct = ct

    def test_create_resolve_revoke_cycle(self):
        row = self.ct.create_user_key("Ada", "ada@example.com")
        self.assertTrue(row["token"].startswith("cgu_"))
        self.assertEqual(row["token_prefix"], row["token"][:12])
        resolved = self.ct.resolve_user_token(row["token"])
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["user_name"], "Ada")
        self.assertEqual(resolved["user_email"], "ada@example.com")
        self.assertTrue(self.ct.revoke_user_key(row["id"]))
        self.assertIsNone(self.ct.resolve_user_token(row["token"]))

    def test_store_keeps_hash_only(self):
        row = self.ct.create_user_key("Bob", "bob@example.com")
        repo = self.ct.get_client_token_repository()
        stored = repo._rows[row["id"]]
        self.assertNotIn(row["token"], str(stored))
        self.assertEqual(stored["token_hash"], self.ct._digest(row["token"]))
        listed = self.ct.list_user_keys()
        self.assertNotIn("token_hash", listed[0])
        self.assertNotIn("token", listed[0])

    def test_resolve_fails_closed(self):
        self.assertIsNone(self.ct.resolve_user_token(""))
        self.assertIsNone(self.ct.resolve_user_token("short"))
        self.assertIsNone(self.ct.resolve_user_token("cgu_" + "x" * 32))
        # revoked key resolves to nothing
        row = self.ct.create_user_key("Cara", "cara@example.com")
        self.ct.revoke_user_key(row["id"])
        self.assertIsNone(self.ct.resolve_user_token(row["token"]))

    def test_negative_cache_absorbs_unknown_probes(self):
        probe = "cgu_" + "z" * 32
        self.assertIsNone(self.ct.resolve_user_token(probe))
        self.assertIn(self.ct._digest(probe), self.ct._neg_cache)
        self.assertIsNone(self.ct.resolve_user_token(probe))


class AdminUserKeyEndpointsTestCase(unittest.TestCase):
    def setUp(self):
        import app.core.client_tokens as ct
        from app.api.routers import admin

        ct._memory_repo = None
        ct._neg_cache.clear()
        self.ct = ct
        self.admin = admin

    def _req(self, token: str | None, ip: str = "127.0.0.1"):
        from starlette.requests import Request

        headers = []
        if token:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/admin/users",
                "headers": headers,
                "client": (ip, 5555),
            }
        )

    def test_endpoints_require_admin_token(self):
        with patch("app.core.config.settings.admin_panel_token", "sekret"):
            with self.assertRaises(Exception) as ctx:
                self.admin.create_user_key(self._req(None, "10.0.0.1"), user_name="A", user_email="a@b.co")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 401)
            with self.assertRaises(Exception) as ctx:
                self.admin.create_user_key(self._req("wrong", "10.0.0.2"), user_name="A", user_email="a@b.co")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 401)
            row = self.admin.create_user_key(self._req("sekret", "10.0.0.3"), user_name="A", user_email="a@b.co")
            self.assertTrue(row["created"])

    def test_create_validates_input(self):
        with patch("app.core.config.settings.admin_panel_token", "sekret"):
            req = self._req("sekret", "10.0.0.4")
            with self.assertRaises(Exception) as ctx:
                self.admin.create_user_key(req, user_name="", user_email="a@b.co")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 422)
            with self.assertRaises(Exception) as ctx:
                self.admin.create_user_key(req, user_name="A", user_email="not-an-email")
            self.assertEqual(getattr(ctx.exception, "status_code", None), 422)

    def test_list_and_revoke(self):
        with patch("app.core.config.settings.admin_panel_token", "sekret"):
            req = self._req("sekret", "10.0.0.5")
            created = self.admin.create_user_key(req, user_name="Dee", user_email="dee@example.com")
            listing = self.admin.list_user_keys(req)
            self.assertEqual(len(listing["users"]), 1)
            self.assertNotIn("token", listing["users"][0])
            out = self.admin.revoke_user_key(req, created["id"])
            self.assertTrue(out["revoked"])
            with self.assertRaises(Exception) as ctx:
                self.admin.revoke_user_key(req, 999999)
            self.assertEqual(getattr(ctx.exception, "status_code", None), 404)
