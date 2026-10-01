"""Rate limits, registration mode and per-tenant upload limits. No Gemini calls.

Limits are switched on per test through environment variables (they are read
per request), so the rest of the suite keeps the development defaults.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_limits -v
"""

import os
import secrets
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import bearer, login, make_user, new_password, unique_email  # noqa: I001  (test JWT secret first)

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.rate_limit import RateLimited, enforce
from src.api.settings import Limit, parse_limit, rate_limits_enabled, registration_open
from src.rag.db import connect
from src.rag.llm import generate_answer
from src.rag.tenants import DEFAULT_TENANT, get_or_create_tenant, get_tenant

DOCS = "/api/v1/documents"
QUERY = "/api/v1/query"
LIMITS_ON = {"RATE_LIMITS_ENABLED": "true"}


def wait_for_fresh_window(seconds: int, margin: float = 5.0) -> None:
    """Fixed windows restart at their boundary (by design). A test that expects the
    N+1th request in a window to be refused must not straddle that boundary, so if
    the current window ends within `margin` seconds, wait for the next one.
    Uses the database clock, which is what the limiter uses."""
    with connect() as conn:
        now = float(conn.execute("SELECT extract(epoch FROM clock_timestamp())").fetchone()[0])
    remaining = seconds - (now % seconds)
    if remaining < margin:
        time.sleep(remaining + 0.25)


def markdown(label: str) -> bytes:
    return f"# Note {label}\n\nThe {label} reference number is {secrets.token_hex(4)}.\n".encode()


class SettingsTests(unittest.TestCase):
    def test_parse_limit(self):
        self.assertEqual(parse_limit("5/minute"), Limit(5, 60))
        self.assertEqual(parse_limit(" 3 / hours "), Limit(3, 3600))
        self.assertEqual(parse_limit("20/day"), Limit(20, 86400))
        self.assertIsNone(parse_limit("off"))
        for bad in ("5", "0/minute", "five/minute", "5/week", "-1/hour"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                parse_limit(bad)

    def test_environment_defaults(self):
        with mock.patch.dict(os.environ, {}):
            for name in ("APP_ENV", "RATE_LIMITS_ENABLED", "REGISTRATION_MODE"):
                os.environ.pop(name, None)
            self.assertFalse(rate_limits_enabled())  # development: unchanged behavior
            self.assertTrue(registration_open())
            os.environ["APP_ENV"] = "production"
            self.assertTrue(rate_limits_enabled())  # production: safe defaults
            self.assertFalse(registration_open())
            os.environ["REGISTRATION_MODE"] = "sometimes"
            with self.assertRaises(ValueError):
                registration_open()

    def test_malformed_setting_stops_startup(self):
        with mock.patch.dict(os.environ, {"RATE_LIMIT_LOGIN": "lots"}):
            with self.assertRaises(ValueError):
                with TestClient(app):
                    pass

    def test_fixed_window_counter(self):
        bucket_a, bucket_b = f"unit:{secrets.token_hex(4)}", f"unit:{secrets.token_hex(4)}"
        wait_for_fresh_window(60)
        try:
            with mock.patch.dict(os.environ, {**LIMITS_ON, "RATE_LIMIT_UPLOADS": "2/minute"}):
                enforce("UPLOADS", bucket_a, "limited")
                enforce("UPLOADS", bucket_a, "limited")
                with self.assertRaises(RateLimited) as caught:
                    enforce("UPLOADS", bucket_a, "limited")
                self.assertTrue(1 <= caught.exception.retry_after_seconds <= 60)
                enforce("UPLOADS", bucket_b, "limited")  # another bucket is independent
            with mock.patch.dict(os.environ, {"RATE_LIMITS_ENABLED": "false", "RATE_LIMIT_UPLOADS": "1/minute"}):
                enforce("UPLOADS", bucket_a, "limited")  # disabled: never raises
        finally:
            with connect() as conn:
                conn.execute("DELETE FROM rate_limits WHERE bucket = ANY(%s)", ([bucket_a, bucket_b],))


class LimitApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = tempfile.mkdtemp(prefix="rag-limits-test-")
        cls.env = mock.patch.dict(os.environ, {"UPLOAD_DIR": str(Path(cls.sandbox) / "uploads")})
        cls.env.start()
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.pipeline = app.state.pipeline
        cls.conn = connect()
        cls.clear_ip_buckets()
        cls.default = get_tenant(cls.conn, DEFAULT_TENANT)
        cls.tenant_ids, cls.user_ids, cls.accounts, cls.tokens = [], [], {}, {}
        for key in ("a", "b"):
            email, password = unique_email(f"limits-{key}"), new_password()
            response = cls.client.post("/api/v1/auth/register", json={
                "organization_name": f"Limits Test {key.upper()}", "email": email, "password": password})
            assert response.status_code == 201, response.text
            cls.tenant_ids.append(get_tenant(cls.conn, response.json()["tenant"]["slug"]).id)
            cls.accounts[key] = (email, password)
            cls.tokens[key] = login(cls.client, email, password)
            # One document each, so both tenants can be queried.
            upload = cls.client.post(DOCS, headers=bearer(cls.tokens[key]),
                                     files={"file": (f"base-{key}.md", markdown(f"base {key}"), "text/markdown")})
            assert upload.status_code == 201, upload.text
        cls.tenants = dict(zip(("a", "b"), cls.tenant_ids))
        user, password = make_user(cls.conn, cls.default.id, "limits-default")
        cls.user_ids.append(user.id)
        cls.tokens["default"] = login(cls.client, user.email, password)
        cls.default_counts = cls.counts(cls.default.id)

    @classmethod
    def tearDownClass(cls):
        cls.clear_ip_buckets()
        cls.clear_tenant_buckets()
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (cls.tenant_ids,))
        cls.conn.execute("DELETE FROM users WHERE id = ANY(%s)", (cls.user_ids,))
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (cls.tenant_ids,))
        assert cls.counts(cls.default.id) == cls.default_counts, "default tenant changed"
        cls.conn.close()
        cls.client_context.__exit__(None, None, None)
        cls.env.stop()
        shutil.rmtree(cls.sandbox, ignore_errors=True)

    def setUp(self):
        self.real_client = self.pipeline.gemini_client
        self.pipeline.generate = lambda client, question, sources: "An answer [1]."  # never a real Gemini call
        self.pipeline.gemini_client = self.real_client or object()
        self.clear_ip_buckets()
        self.clear_tenant_buckets()

    def tearDown(self):
        self.pipeline.generate, self.pipeline.gemini_client = generate_answer, self.real_client

    # --- helpers ------------------------------------------------------------------
    @classmethod
    def clear_ip_buckets(cls):
        cls.conn.execute("DELETE FROM rate_limits WHERE bucket = 'register:testclient' OR bucket LIKE 'login:testclient:%'")

    @classmethod
    def clear_tenant_buckets(cls):
        ids = getattr(cls, "tenant_ids", []) + [get_tenant(cls.conn, DEFAULT_TENANT).id]
        cls.conn.execute("DELETE FROM rate_limits WHERE bucket = ANY(%s)",
                         ([f"{kind}:tenant:{t}" for t in ids for kind in ("query", "answer", "upload")],))

    @classmethod
    def counts(cls, tenant_id):
        return cls.conn.execute("SELECT count(*), (SELECT count(*) FROM document_chunks WHERE tenant_id = %s) "
                                "FROM documents WHERE tenant_id = %s", (tenant_id, tenant_id)).fetchone()

    def uploaded(self, key) -> list[dict]:
        items = self.client.get(DOCS, headers=bearer(self.tokens[key]), params={"limit": 200}).json()["items"]
        return [d for d in items if d["origin"] == "upload"]

    def stored_files(self, key) -> int:
        folder = Path(self.sandbox) / "uploads" / str(self.tenants[key])
        return len([p for p in folder.glob("*") if p.is_file()]) if folder.exists() else 0

    def upload(self, key, label):
        return self.client.post(DOCS, headers=bearer(self.tokens[key]),
                                files={"file": (f"{label}.md", markdown(label), "text/markdown")})

    def remove_extra_uploads(self, key):
        for document in self.uploaded(key):
            if not document["filename"].startswith("base-"):
                self.client.delete(f"{DOCS}/{document['id']}", headers=bearer(self.tokens[key]))

    def assert_rate_limited(self, response, code="rate_limited", window=60):
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.json()["error"]["code"], code)
        retry_after = int(response.headers["Retry-After"])
        self.assertTrue(1 <= retry_after <= window + 1, retry_after)
        self.assertTrue(0 < response.json()["error"]["retry_after_seconds"] <= window)

    # --- login ----------------------------------------------------------------------
    def test_login_is_limited_per_ip_and_email(self):
        email, password = self.accounts["a"]
        wait_for_fresh_window(60)
        with mock.patch.dict(os.environ, {**LIMITS_ON, "RATE_LIMIT_LOGIN": "3/minute"}):
            for _ in range(3):
                self.assertEqual(self.client.post("/api/v1/auth/login",
                                                  json={"email": email, "password": "wrong-" + new_password()}).status_code, 401)
            # The 4th attempt is refused even with the right password.
            self.assert_rate_limited(self.client.post("/api/v1/auth/login", json={"email": email, "password": password}))
            # A different email from the same IP has its own counter.
            other_email, other_password = self.accounts["b"]
            self.assertEqual(self.client.post("/api/v1/auth/login",
                                              json={"email": other_email, "password": other_password}).status_code, 200)

    # --- registration ----------------------------------------------------------------
    def register(self, label):
        return self.client.post("/api/v1/auth/register", json={
            "organization_name": f"Limits Reg {label}", "email": unique_email(f"limits-reg-{label}"),
            "password": new_password()})

    def forget_registered(self, response):
        if response.status_code == 201:
            self.tenant_ids.append(get_tenant(self.conn, response.json()["tenant"]["slug"]).id)

    def test_registration_is_limited_per_ip(self):
        wait_for_fresh_window(3600)
        with mock.patch.dict(os.environ, {**LIMITS_ON, "RATE_LIMIT_REGISTER": "2/hour"}):
            for label in ("r1", "r2"):
                response = self.register(label)
                self.forget_registered(response)
                self.assertEqual(response.status_code, 201)
            tenants_before = self.conn.execute("SELECT count(*) FROM tenants").fetchone()[0]
            self.assert_rate_limited(self.register("r3"), window=3600)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM tenants").fetchone()[0], tenants_before)

    def test_registration_closed_and_open(self):
        tenants_before = self.conn.execute("SELECT count(*) FROM tenants").fetchone()[0]
        with mock.patch.dict(os.environ, {"REGISTRATION_MODE": "closed"}):
            response = self.register("closed")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "registration_closed")
            self.assertIn("administrator", response.json()["error"]["message"])
            self.assertEqual(self.conn.execute("SELECT count(*) FROM tenants").fetchone()[0], tenants_before)
            # Existing users keep working while registration is closed.
            email, password = self.accounts["a"]
            self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": email, "password": password}).status_code, 200)
        with mock.patch.dict(os.environ, {"REGISTRATION_MODE": "open"}):
            response = self.register("open")
            self.forget_registered(response)
            self.assertEqual(response.status_code, 201)

    def test_production_closes_registration_by_default(self):
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}):
            os.environ.pop("REGISTRATION_MODE", None)
            response = self.register("prod-default")
            self.assertEqual(response.status_code, 403)

    # --- queries and answers ----------------------------------------------------------
    def query(self, key, retrieve_only=True):
        return self.client.post(QUERY, headers=bearer(self.tokens[key]),
                                json={"question": "What is the base reference number?", "retrieve_only": retrieve_only})

    def test_query_limit_is_per_tenant(self):
        wait_for_fresh_window(60)
        with mock.patch.dict(os.environ, {**LIMITS_ON, "RATE_LIMIT_QUERIES": "2/minute"}):
            self.assertEqual(self.query("a").status_code, 200)
            self.assertEqual(self.query("a").status_code, 200)
            self.assert_rate_limited(self.query("a"))
            # Tenant B is unaffected by tenant A's usage.
            self.assertEqual(self.query("b").status_code, 200)

    def test_answer_limit_spares_sources_only_queries(self):
        wait_for_fresh_window(86400)
        with mock.patch.dict(os.environ, {**LIMITS_ON, "RATE_LIMIT_ANSWERS": "1/day", "RATE_LIMIT_QUERIES": "100/minute"}):
            self.assertEqual(self.query("a", retrieve_only=False).status_code, 200)
            self.assert_rate_limited(self.query("a", retrieve_only=False), code="answer_limit_reached", window=86400)
            self.assertEqual(self.query("a", retrieve_only=True).status_code, 200)
            self.assertEqual(self.query("b", retrieve_only=False).status_code, 200)

    def test_limits_do_nothing_when_disabled(self):
        with mock.patch.dict(os.environ, {"RATE_LIMITS_ENABLED": "false", "RATE_LIMIT_QUERIES": "1/minute"}):
            for _ in range(3):
                self.assertEqual(self.query("a").status_code, 200)

    # --- uploads ------------------------------------------------------------------------
    def test_upload_rate_limit_is_per_tenant(self):
        wait_for_fresh_window(3600)
        try:
            with mock.patch.dict(os.environ, {**LIMITS_ON, "RATE_LIMIT_UPLOADS": "1/hour"}):
                self.assertEqual(self.upload("a", "rate-1").status_code, 201)
                files_before = self.stored_files("a")
                self.assert_rate_limited(self.upload("a", "rate-2"), window=3600)
                self.assertEqual(self.stored_files("a"), files_before)
                self.assertEqual(self.upload("b", "rate-1").status_code, 201)
        finally:
            self.remove_extra_uploads("a")
            self.remove_extra_uploads("b")

    # --- tenant limits -------------------------------------------------------------------
    def test_document_count_limit(self):
        try:
            with mock.patch.dict(os.environ, {"TENANT_MAX_DOCUMENTS": "2"}):  # A already has 1 upload
                self.assertEqual(self.upload("a", "count-1").status_code, 201)
                files_before = self.stored_files("a")
                response = self.upload("a", "count-2")
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"]["code"], "tenant_document_limit_reached")
                self.assertEqual(self.stored_files("a"), files_before)  # rejected before anything was stored
                self.assertEqual(self.upload("b", "count-1").status_code, 201)  # B has its own count
                # Deleting a document frees a slot.
                extra = next(d for d in self.uploaded("a") if d["filename"] == "count-1.md")
                self.assertEqual(self.client.delete(f"{DOCS}/{extra['id']}", headers=bearer(self.tokens["a"])).status_code, 204)
                self.assertEqual(self.upload("a", "count-3").status_code, 201)
        finally:
            self.remove_extra_uploads("a")
            self.remove_extra_uploads("b")

    def test_uploaded_bytes_limit(self):
        used = self.conn.execute("SELECT coalesce(sum(size_bytes), 0) FROM documents WHERE tenant_id = %s AND origin = 'upload'",
                                 (self.tenants["a"],)).fetchone()[0]
        self.assertGreater(used, 0)  # sizes are recorded for uploads
        first, second = markdown("bytes-1"), markdown("bytes-2")
        try:
            with mock.patch.dict(os.environ, {"TENANT_MAX_UPLOAD_BYTES": str(used + len(first) + 10)}):
                self.assertEqual(self.client.post(DOCS, headers=bearer(self.tokens["a"]),
                                                  files={"file": ("bytes-1.md", first, "text/markdown")}).status_code, 201)
                response = self.client.post(DOCS, headers=bearer(self.tokens["a"]),
                                            files={"file": ("bytes-2.md", second, "text/markdown")})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"]["code"], "tenant_storage_limit_reached")
                self.assertEqual(self.upload("b", "bytes-1").status_code, 201)  # B is separate
            stored = self.conn.execute("SELECT size_bytes FROM documents WHERE tenant_id = %s AND source = 'bytes-1.md'",
                                       (self.tenants["a"],)).fetchone()[0]
            self.assertEqual(stored, len(first))
        finally:
            self.remove_extra_uploads("a")
            self.remove_extra_uploads("b")

    def test_folder_documents_do_not_count(self):
        # Folder-ingested documents never use the upload allowance: a tenant holding only
        # folder documents can still upload with a limit of 1. The tenant is this test's own
        # (it neither depends on nor writes to the default tenant).
        tenant_id = get_or_create_tenant(self.conn, f"limits-folder-{secrets.token_hex(3)}", "Limits folder").id
        self.tenant_ids.append(tenant_id)  # removed in tearDownClass with its users and documents
        for i in range(3):
            self.conn.execute(
                """INSERT INTO documents (tenant_id, relative_path, source, source_type, path, content_hash, chunk_size,
                                          chunk_overlap, embedding_model, embedding_input_version, origin)
                   VALUES (%s, %s, %s, 'local', %s, repeat('0', 64), 500, 100, 'test-model', 'test', 'folder')""",
                (tenant_id, f"folder-{i}.md", f"folder-{i}.md", f"folder-{i}.md"))
        user, password = make_user(self.conn, tenant_id, "limits-folder")
        token = login(self.client, user.email, password)
        before = self.counts(tenant_id)
        with mock.patch.dict(os.environ, {"TENANT_MAX_DOCUMENTS": "1", "TENANT_MAX_UPLOAD_BYTES": "100000"}):
            response = self.client.post(DOCS, headers=bearer(token),
                                        files={"file": ("folder-tenant-upload.md", markdown("folder tenant"), "text/markdown")})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.client.delete(f"{DOCS}/{response.json()['id']}", headers=bearer(token)).status_code, 204)
        self.assertEqual(self.counts(tenant_id), before)  # the three folder documents are untouched

    def test_concurrent_uploads_cannot_exceed_the_document_limit(self):
        results = []

        def send(label):
            results.append(self.upload("a", label).status_code)

        try:
            with mock.patch.dict(os.environ, {"TENANT_MAX_DOCUMENTS": "2"}):  # room for exactly one more
                threads = [threading.Thread(target=send, args=(f"race-{i}",)) for i in range(3)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(sorted(results), [201, 409, 409])
            self.assertEqual(len(self.uploaded("a")), 2)
        finally:
            self.remove_extra_uploads("a")


if __name__ == "__main__":
    unittest.main()
