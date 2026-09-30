"""Backup and restore verification: pg_dump -> restore into a scratch database -> compare -> use it.

A throwaway company (its own tenant: admin + employees, departments, memberships,
company-wide / department / admin-only documents) is added next to the real
corpus, so the backup contains real authorization data. The whole database is
dumped (read-only), restored into a NEW scratch database, compared check by
check, and then the API itself is started against the restored copy: logins
with the original passwords, /auth/me, AccessScope, authorized retrieval,
document listing and the Admin API. Writes during those checks go to the scratch
database only, which is dropped at the end. The application database is never
restored over, dropped or truncated. No Gemini calls.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_backup_restore -v
"""

import os
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

import psycopg
from fastapi.testclient import TestClient
from pgvector.psycopg import register_vector

from src.api.main import app
from src.rag import backup
from src.rag.access import AccessScope
from src.rag.chunking import chunk_document
from src.rag.db import connect
from src.rag.embeddings import load_model
from src.rag.ingest import sync_documents
from src.rag.loader import load_documents
from src.rag.retriever import PgVectorRetriever
from src.rag.tenants import DEFAULT_TENANT, get_or_create_tenant, get_tenant

SUFFIX = secrets.token_hex(4)
QUESTION = "What is the Osprey vault code?"
SECRET = {name: f"{name.upper()}-{secrets.token_hex(3).upper()}" for name in ("company", "finance", "board")}
CORPUS_QUESTION = "How many annual leave days do employees receive?"


def can_create_databases() -> bool:
    with connect() as conn:
        return bool(conn.execute("SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()[0])


class ScratchNameSafetyTests(unittest.TestCase):
    """The guards that keep every destructive step away from the application database."""

    def test_only_scratch_names_are_accepted(self):
        app_db = backup.app_database()
        for name in (app_db, "postgres", "template0", "template1", "youtube_trending_db", "",
                     f"{app_db}_restore_verify_", f"{app_db}_restore_verify_XYZ12345",
                     f"{app_db}_restore_verify_0123456789", "x; DROP DATABASE postgres"):
            with self.subTest(name=name), self.assertRaises(backup.BackupError):
                backup.check_scratch_name(name, app_db)
        name = backup.new_scratch_name(app_db)
        self.assertEqual(backup.check_scratch_name(name, app_db), name)
        self.assertNotEqual(name, app_db)

    def test_destructive_steps_refuse_the_application_database_before_connecting(self):
        app_db = backup.app_database()
        with mock.patch.object(psycopg, "connect") as connect_mock, mock.patch("subprocess.run") as run_mock:
            for step in (backup.create_scratch_database, backup.drop_scratch_database,
                         lambda name: backup.restore_into(Path("x.dump"), name)):
                for name in (app_db, "postgres"):
                    with self.subTest(step=getattr(step, "__name__", "restore_into"), name=name):
                        with self.assertRaises(backup.BackupError):
                            step(name)
            connect_mock.assert_not_called()
            run_mock.assert_not_called()

    def test_existing_backup_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "existing.dump"
            existing.write_bytes(b"keep me")
            with self.assertRaises(backup.BackupError):
                backup.create_backup(existing)
            self.assertEqual(existing.read_bytes(), b"keep me")


@unittest.skipUnless(can_create_databases(), "the database role cannot create databases")
class BackupRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rag-backup-test-")
        cls.app_db = backup.app_database()
        cls.conn = connect()
        register_vector(cls.conn)
        assert cls.conn.execute("SELECT current_database()").fetchone()[0] == cls.app_db

        # --- a throwaway company with real authorization data, in the application database ---
        cls.tenant = get_or_create_tenant(cls.conn, f"test-backup-{SUFFIX}", "Backup Co").id
        folder = Path(cls.tmp) / "docs"
        for path, key in (("company.md", "company"), ("finance/budget.md", "finance"), ("board/minutes.md", "board")):
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_text(f"# Osprey vault ({key})\n\nThe Osprey vault code for {key} is {SECRET[key]}.\n",
                                       encoding="utf-8")
        documents = load_documents(folder)
        stats = sync_documents(cls.conn, load_model(), documents, {d.relative_path: chunk_document(d) for d in documents},
                               500, 100, tenant_id=cls.tenant)
        assert not stats.failed, stats.failed
        doc = {p: cls.conn.execute("SELECT id FROM documents WHERE tenant_id = %s AND relative_path = %s",
                                   (cls.tenant, p)).fetchone()[0] for p in ("company.md", "finance/budget.md", "board/minutes.md")}
        cls.doc = doc
        cls.finance = cls.conn.execute("INSERT INTO departments (tenant_id, slug, name) VALUES (%s, 'finance', 'Finance') "
                                       "RETURNING id", (cls.tenant,)).fetchone()[0]
        cls.conn.execute("UPDATE documents SET visibility = 'departments' WHERE id = ANY(%s)",
                         ([doc["finance/budget.md"], doc["board/minutes.md"]],))
        cls.conn.execute("INSERT INTO document_departments (tenant_id, document_id, department_id) VALUES (%s, %s, %s)",
                         (cls.tenant, doc["finance/budget.md"], cls.finance))
        cls.users = {}
        for name, role in (("admin", "admin"), ("fin", "employee"), ("eng", "employee")):
            user, password = make_user(cls.conn, cls.tenant, f"backup-{name}")
            cls.conn.execute("UPDATE users SET role = %s WHERE id = %s", (role, user.id))
            cls.users[name] = (user, password)
        cls.conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s)",
                         (cls.tenant, cls.users["fin"][0].id, cls.finance))

        # --- backup (read-only) and restore into a new scratch database ---
        cls.dump = Path(cls.tmp) / "backup.dump"
        cls.info = backup.create_backup(cls.dump)
        cls.source = backup.fingerprint(cls.conn)
        cls.scratch = backup.new_scratch_name(cls.app_db)
        backup.create_scratch_database(cls.scratch)
        backup.restore_into(cls.dump, cls.scratch)
        cls.restored_conn = psycopg.connect(dbname=cls.scratch, autocommit=True)
        register_vector(cls.restored_conn)
        assert cls.restored_conn.execute("SELECT current_database()").fetchone()[0] == cls.scratch
        cls.restored = backup.fingerprint(cls.restored_conn)

    @classmethod
    def tearDownClass(cls):
        cls.restored_conn.close()
        backup.drop_scratch_database(cls.scratch)
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = %s", (cls.tenant,))
        cls.conn.execute("DELETE FROM tenants WHERE id = %s", (cls.tenant,))  # users, departments, links cascade
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- the archive and the restored copy ------------------------------------------------------------
    def test_archive_contains_every_application_table(self):
        self.assertEqual(set(self.info.tables_with_data), set(backup.APP_TABLES))
        self.assertGreater(self.info.size_bytes, 1_000_000)
        self.assertEqual(len(self.info.sha256), 64)

    def test_restored_database_is_identical(self):
        self.assertEqual(backup.compare(self.source, self.restored), [])
        # The comparison really covers authorization data (not just empty tables).
        for table in ("departments", "user_departments", "document_departments"):
            self.assertGreater(self.restored[f"count {table}"], 0)
        default = self.restored_conn.execute(
            "SELECT count(*), (SELECT count(*) FROM document_chunks c JOIN tenants t ON t.id = c.tenant_id "
            "WHERE t.slug = %s) FROM documents d JOIN tenants t ON t.id = d.tenant_id WHERE t.slug = %s",
            (DEFAULT_TENANT, DEFAULT_TENANT)).fetchone()
        self.assertEqual(default, (1224, 22018))

    def test_restored_indexes_and_vector_search_work(self):
        indexes = {r[0] for r in self.restored_conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
        self.assertTrue({"document_chunks_lexical_vector_idx", "documents_id_tenant_key", "users_id_tenant_key",
                         "document_chunks_tenant_chunk_id_key"} <= indexes)
        model = load_model()
        tenant = get_tenant(self.conn, DEFAULT_TENANT).id
        live = PgVectorRetriever(self.conn, model, None, scope=AccessScope.operator(tenant)).search(CORPUS_QUESTION, top_k=5)
        copy = PgVectorRetriever(self.restored_conn, model, None, scope=AccessScope.operator(tenant)).search(CORPUS_QUESTION, top_k=5)
        self.assertEqual([(r.chunk_id, round(r.score, 6)) for r in copy], [(r.chunk_id, round(r.score, 6)) for r in live])
        hits = self.restored_conn.execute(
            "SELECT count(*) FROM document_chunks WHERE lexical_vector @@ plainto_tsquery('english', 'annual leave')").fetchone()[0]
        self.assertGreater(hits, 0)

    def test_dump_contains_no_secrets_and_the_check_itself_works(self):
        self.assertEqual(backup.secrets_in_dump(self.dump), [])
        # Positive control: a value that IS in the dump (a chunk's text) is detected, without printing it.
        with mock.patch.dict(os.environ, {"JWT_SECRET_KEY": SECRET["finance"]}):
            self.assertEqual(backup.secrets_in_dump(self.dump), ["JWT_SECRET_KEY"])

    # --- the application running on the restored copy ---------------------------------------------------
    def test_application_works_on_the_restored_database(self):
        before_live = backup.fingerprint(self.conn)
        with mock.patch.dict(os.environ, {"PGDATABASE": self.scratch}):
            # The owner identity (the restored copy has no role grants); empty, so the API's
            # load_dotenv(.env) cannot fill APP_DB_USER back in.
            os.environ["APP_DB_USER"] = ""
            with TestClient(app) as client:
                with psycopg.connect() as probe:  # what the application now connects to
                    self.assertEqual(probe.execute("SELECT current_database()").fetchone()[0], self.scratch)
                tokens = {name: login(client, user.email, password) for name, (user, password) in self.users.items()}

                # /auth/me and the AccessScope come from the restored rows.
                me = {name: client.get("/api/v1/auth/me", headers=bearer(t)).json() for name, t in tokens.items()}
                self.assertEqual((me["admin"]["role"], me["fin"]["role"]), ("admin", "employee"))
                self.assertEqual([d["name"] for d in me["fin"]["departments"]], ["Finance"])
                self.assertEqual(me["eng"]["departments"], [])

                def texts(name):
                    response = client.post("/api/v1/query", headers=bearer(tokens[name]),
                                           json={"question": QUESTION, "top_k": 10, "retrieve_only": True})
                    self.assertEqual(response.status_code, 200, response.text)
                    return "\n".join(s["text"] for s in response.json()["sources"])

                def listed(name):
                    return {i["filename"] for i in client.get("/api/v1/documents", headers=bearer(tokens[name]),
                                                               params={"limit": 200}).json()["items"]}

                # Authorized retrieval and listing, exactly as before the backup.
                expected = {"admin": {"company", "finance", "board"}, "fin": {"company", "finance"}, "eng": {"company"}}
                for name, visible in expected.items():
                    with self.subTest(user=name):
                        text = texts(name)
                        for key in SECRET:
                            (self.assertIn if key in visible else self.assertNotIn)(SECRET[key], text)
                        self.assertEqual(listed(name), {f"{'company' if k == 'company' else ('budget' if k == 'finance' else 'minutes')}.md"
                                                        for k in visible})

                # Admin API authorization.
                self.assertEqual(client.get("/api/v1/admin/users", headers=bearer(tokens["eng"])).status_code, 403)
                users = client.get("/api/v1/admin/users", headers=bearer(tokens["admin"]))
                self.assertEqual((users.status_code, users.json()["total"]), (200, 3))
                changed = client.put(f"/api/v1/admin/users/{self.users['eng'][0].id}/departments/{self.finance}",
                                     headers=bearer(tokens["admin"]))
                self.assertEqual(changed.status_code, 200)
                self.assertIn(SECRET["finance"], texts("eng"))  # the change applies to the restored copy...
        # ...and never reached the application database.
        self.assertEqual(backup.fingerprint(self.conn), before_live)
        self.assertNotIn(SECRET["finance"], "\n".join(
            r.text for r in PgVectorRetriever(self.conn, load_model(), None, scope=AccessScope(
                self.tenant, self.users["eng"][0].id, "employee")).search(QUESTION, top_k=10)))


if __name__ == "__main__":
    unittest.main()
