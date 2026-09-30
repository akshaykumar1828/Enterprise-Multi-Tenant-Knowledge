"""Batch 3 reliability tests: pool/timeouts, production validation, request IDs and
logging, production health, offline model loading. No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_reliability -v
"""

import json
import logging
import os
import re
import secrets
import socket
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import bearer, login, make_user  # noqa: I001  (test JWT secret first)

import psycopg
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.api.logging_setup import JsonFormatter, redact, request_id_from
from src.api.settings import ProductionConfigError, jwt_secret_problems, validate_production_settings
from src.rag import db
from src.rag.db import app_connection_kwargs, connect
from src.rag.llm import generate_answer
from src.rag.tenants import DEFAULT_TENANT, get_tenant

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEX_ID = re.compile(r"^[0-9a-f]{32}$")


class ApiReliabilityTests(unittest.TestCase):
    """One running app (development settings) shared by the request-level tests."""

    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(api_main.app)
        cls.client = cls.client_context.__enter__()
        cls.pipeline = api_main.app.state.pipeline
        cls.real_gemini = cls.pipeline.gemini_client
        with connect() as conn:
            cls.user, password = make_user(conn, get_tenant(conn, DEFAULT_TENANT).id, "reliability")
        cls.password = password
        cls.token = login(cls.client, cls.user.email, password)

    @classmethod
    def tearDownClass(cls):
        with connect() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (cls.user.id,))
        cls.client_context.__exit__(None, None, None)

    def setUp(self):
        self.pipeline.generate = lambda client, question, sources: "unused"  # never a real Gemini call

    def tearDown(self):
        self.pipeline.generate, self.pipeline.gemini_client = generate_answer, self.real_gemini

    # --- pool and timeouts ------------------------------------------------------------
    def test_pool_is_open_with_conservative_defaults(self):
        pool = db.app_pool()
        self.assertIsNotNone(pool)
        self.assertFalse(pool.closed)
        self.assertEqual((pool.min_size, pool.max_size, pool.timeout), (1, 10, 5))

    def test_api_connections_carry_connect_and_statement_timeouts(self):
        with db.connect_app() as conn:
            self.assertEqual(conn.execute("SHOW statement_timeout").fetchone()[0], "15s")
            parameters = conn.info.get_parameters()
            self.assertEqual(parameters.get("connect_timeout"), "5")
        # Operator connections are unchanged (no statement timeout added).
        with connect() as owner:
            self.assertEqual(owner.execute("SHOW statement_timeout").fetchone()[0], "0")

    def test_api_connections_disable_parallel_query_workers(self):
        with db.connect_app() as conn:  # from the pool
            self.assertEqual(conn.execute("SHOW max_parallel_workers_per_gather").fetchone()[0], "0")
        with psycopg.connect(**app_connection_kwargs()) as conn:  # short-lived API connection
            self.assertEqual(conn.execute("SHOW max_parallel_workers_per_gather").fetchone()[0], "0")
        # Operator connections keep the server's own setting: nothing is set by the client.
        with connect() as owner:
            source = owner.execute(
                "SELECT source FROM pg_settings WHERE name = 'max_parallel_workers_per_gather'").fetchone()[0]
            self.assertNotEqual(source, "client")

    def test_statement_timeout_cancels_slow_queries(self):
        with mock.patch.dict(os.environ, {"DB_STATEMENT_TIMEOUT_MS": "300"}):
            with psycopg.connect(**app_connection_kwargs()) as conn:
                with self.assertRaises(psycopg.errors.QueryCanceled):
                    conn.execute("SELECT pg_sleep(2)")

    def test_statement_timeout_becomes_503(self):
        with mock.patch.object(self.pipeline, "answer", side_effect=psycopg.errors.QueryCanceled("canceled")):
            response = self.client.post("/api/v1/query", json={"question": "anything", "retrieve_only": True},
                                        headers=bearer(self.token))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "database_timeout")

    def test_exhausted_pool_becomes_503_with_retry_after(self):
        db.close_app_pool()
        try:
            with mock.patch.dict(os.environ, {"DB_POOL_MAX_SIZE": "1", "DB_POOL_TIMEOUT": "1"}):
                db.open_app_pool()
            with db.app_pool().connection():  # hold the only connection
                response = self.client.get("/api/v1/auth/me", headers=bearer(self.token))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "database_busy")
            self.assertEqual(response.headers["Retry-After"], "3")
            # Once released, requests succeed again.
            self.assertEqual(self.client.get("/api/v1/auth/me", headers=bearer(self.token)).status_code, 200)
        finally:
            db.close_app_pool()
            db.open_app_pool()

    # --- request IDs -------------------------------------------------------------------
    def test_every_response_has_a_request_id(self):
        first = self.client.get("/api/v1/health").headers["X-Request-ID"]
        second = self.client.get("/api/v1/health").headers["X-Request-ID"]
        self.assertRegex(first, HEX_ID)
        self.assertNotEqual(first, second)
        unauthorized = self.client.get("/api/v1/auth/me")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertRegex(unauthorized.headers["X-Request-ID"], HEX_ID)

    def test_safe_incoming_request_ids_are_reused_and_unsafe_ones_replaced(self):
        good = "caddy-7f3a9b2c.01"
        self.assertEqual(self.client.get("/api/v1/health", headers={"X-Request-ID": good}).headers["X-Request-ID"], good)
        for bad in ("short", "has spaces in it", "<script>alert(1)</script>", "x" * 65, "line\\nbreak-12345"):
            with self.subTest(value=bad):
                returned = self.client.get("/api/v1/health", headers={"X-Request-ID": bad}).headers["X-Request-ID"]
                self.assertRegex(returned, HEX_ID)
        self.assertRegex(request_id_from(None), HEX_ID)

    # --- logging -------------------------------------------------------------------------
    def test_request_log_line_has_request_id_and_no_sensitive_data(self):
        with mock.patch.dict(os.environ, {"LOG_REQUESTS": "true"}):
            with self.assertLogs(level="DEBUG") as captured:
                login_response = self.client.post("/api/v1/auth/login",
                                                  json={"email": self.user.email, "password": self.password})
                response = self.client.get("/api/v1/auth/me?limit=1&secret=abc", headers=bearer(self.token))
        formatter = JsonFormatter()
        lines = [json.loads(formatter.format(record)) for record in captured.records]
        request_lines = [line for line in lines if line["logger"] == "rag.api.request"]
        me_line = next(line for line in request_lines if line["path"] == "/api/v1/auth/me")
        self.assertEqual(me_line["request_id"], response.headers["X-Request-ID"])
        self.assertEqual((me_line["method"], me_line["status"]), ("GET", 200))
        self.assertIsInstance(me_line["duration_ms"], float)
        everything = "\n".join(formatter.format(record) for record in captured.records)
        for secret in (self.password, self.token, login_response.json()["access_token"], "secret=abc", "limit=1"):
            self.assertNotIn(secret, everything)

    def test_unexpected_errors_return_a_generic_500_with_request_id(self):
        with mock.patch.object(self.pipeline, "answer", side_effect=RuntimeError("boom password=hunter2")):
            with self.assertLogs("rag.api", level="ERROR") as captured:
                response = self.client.post("/api/v1/query", json={"question": "anything", "retrieve_only": True},
                                            headers=bearer(self.token))
        self.assertEqual(response.status_code, 500)
        body = response.json()["error"]
        self.assertEqual(body["code"], "internal_error")
        self.assertEqual(body["request_id"], response.headers["X-Request-ID"])
        self.assertNotIn("boom", response.text)  # internals stay in the log
        logged = JsonFormatter().format(captured.records[0])
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("hunter2", logged)  # redacted even inside the traceback

    # --- health ----------------------------------------------------------------------------
    def test_production_health_is_minimal_with_three_states(self):
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}):
            self.pipeline.gemini_client = object()
            response = self.client.get("/api/v1/health")
            self.assertEqual((response.status_code, response.json()), (200, {"status": "ok"}))
            self.pipeline.gemini_client = None
            response = self.client.get("/api/v1/health")
            self.assertEqual((response.status_code, response.json()), (200, {"status": "degraded"}))
            with mock.patch.object(api_main, "connect_app", side_effect=psycopg.OperationalError("down")):
                response = self.client.get("/api/v1/health")
            self.assertEqual((response.status_code, response.json()), (503, {"status": "unavailable"}))
            for word in ("database", "model", "cuda", "device", "gemini", "documents"):
                self.assertNotIn(word, response.text.lower())

    def test_development_health_reports_unavailable_database(self):
        with mock.patch.object(self.pipeline, "corpus_counts", side_effect=psycopg.OperationalError("down")):
            response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unavailable")
        self.assertFalse(response.json()["database"]["ok"])


class LifecycleTests(unittest.TestCase):
    def test_pool_opens_and_closes_with_the_app(self):
        with TestClient(api_main.app):
            pool = db.app_pool()
            self.assertIsNotNone(pool)
            self.assertFalse(pool.closed)
        self.assertTrue(pool.closed)
        self.assertIsNone(db.app_pool())

    def test_bad_pool_settings_stop_startup(self):
        for env in ({"DB_POOL_MAX_SIZE": "0"}, {"DB_POOL_MIN_SIZE": "5", "DB_POOL_MAX_SIZE": "2"},
                    {"DB_STATEMENT_TIMEOUT_MS": "soon"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                with self.assertRaises(ValueError):
                    with TestClient(api_main.app):
                        pass
        self.assertIsNone(db.app_pool())


class ProductionValidationTests(unittest.TestCase):
    def test_jwt_secret_rules(self):
        self.assertEqual(jwt_secret_problems(secrets.token_urlsafe(64)), [])
        cases = {
            "too short": secrets.token_urlsafe(16),
            "low variety": "ab" * 40,
            "placeholder": "please-change-this-secret-before-going-to-production-0123456789",
            "copied example": "example_jwt_secret_key_for_documentation_only_1234567890_abc",
        }
        for name, key in cases.items():
            with self.subTest(case=name):
                problems = jwt_secret_problems(key)
                self.assertTrue(problems)
                self.assertNotIn(key, " ".join(problems))  # never echo the secret

    def test_all_problems_are_reported_without_values(self):
        bad_secret = "change-me-change-me-change-me-change-me-change-me"
        with mock.patch.dict(os.environ, {"APP_ENV": "production", "JWT_SECRET_KEY": bad_secret,
                                          "ACCESS_TOKEN_EXPIRE_MINUTES": "100000", "DB_POOL_MAX_SIZE": "0"}):
            os.environ.pop("APP_DB_USER", None)
            with self.assertRaises(ProductionConfigError) as caught:
                validate_production_settings()
        message = str(caught.exception)
        for expected in ("JWT_SECRET_KEY", "APP_DB_USER", "ACCESS_TOKEN_EXPIRE_MINUTES", "DB_POOL_MAX_SIZE"):
            self.assertIn(expected, message)
        self.assertNotIn(bad_secret, message)

    def test_good_settings_pass_with_warnings(self):
        with mock.patch.dict(os.environ, {"APP_ENV": "production", "JWT_SECRET_KEY": secrets.token_urlsafe(64),
                                          "APP_DB_USER": "rag_app", "APP_DB_PASSWORD": "x"}):
            os.environ.pop("HF_HUB_OFFLINE", None)
            warnings = validate_production_settings()
        self.assertTrue(any("HF_HUB_OFFLINE" in warning for warning in warnings))

    def test_production_startup_refuses_a_placeholder_secret(self):
        env = {"APP_ENV": "production", "APP_DB_USER": "rag_app", "APP_DB_PASSWORD": "x",
               "JWT_SECRET_KEY": "please-change-this-secret-before-going-to-production-0123456789"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(ProductionConfigError):
                with TestClient(api_main.app):
                    pass
        self.assertIsNone(db.app_pool())  # the pool is closed again after a failed start

    def test_production_api_runs_one_worker_without_uvicorn_access_log(self):
        script = (PROJECT_ROOT / "deploy" / "run_api.ps1").read_text(encoding="utf-8")
        self.assertIn("--workers 1", script)
        self.assertIn("--no-access-log", script)
        self.assertIn("--host 127.0.0.1", script)


class LoggingUnitTests(unittest.TestCase):
    def test_redaction(self):
        jwt_like = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl"
        text = redact(f"Authorization: Bearer {jwt_like} key=AIza{'x' * 30} password=hunter2 api_key: abc123")
        for secret in (jwt_like, "AIza" + "x" * 30, "hunter2", "abc123"):
            self.assertNotIn(secret, text)

    def test_json_formatter_emits_one_json_object(self):
        record = logging.LogRecord("rag.test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        line = JsonFormatter().format(record)
        entry = json.loads(line)
        self.assertEqual((entry["level"], entry["logger"], entry["message"]), ("INFO", "rag.test", "hello world"))
        self.assertIn("request_id", entry)
        self.assertNotIn("\n", line)


class OfflineModelTests(unittest.TestCase):
    def test_models_load_without_network_when_offline(self):
        from src.rag.embeddings import embed_texts, hf_offline, load_model
        from src.rag.retriever import load_reranker

        def no_network(*args, **kwargs):
            raise OSError("network access attempted while HF_HUB_OFFLINE=1")

        with mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), \
                mock.patch.object(socket.socket, "connect", no_network), \
                mock.patch.object(socket, "create_connection", no_network), \
                mock.patch.object(socket, "getaddrinfo", no_network):
            self.assertTrue(hf_offline())
            model = load_model()
            reranker = load_reranker()
            self.assertEqual(embed_texts(model, ["offline check"]).shape, (1, 384))
            self.assertEqual(len(reranker.predict([("question", "passage")])), 1)
        with mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "0"}):
            self.assertFalse(hf_offline())


if __name__ == "__main__":
    unittest.main()
