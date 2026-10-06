"""Admin API: departments, roles, memberships and document access. No Gemini calls.

Tenant A: an admin, two employees, departments "finance" and "legal", and two
ingested documents that answer the same question (company.md, vault.md, each
with its own secret). Tenant B: an admin, a department and a document. Every
test starts from that baseline (setUp resets roles, memberships and access).
Admin status comes only from the database; changes apply to the next request
made with the same token.

Run from the project root:
    .venv\\Scripts\\python.exe -m unittest tests.test_admin -v
"""

import os
import secrets
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.helpers import bearer, login, make_user  # noqa: I001  (sets the test JWT secret first)

import jwt
from fastapi.testclient import TestClient
from pgvector.psycopg import register_vector

from src.api.auth import ALGORITHM
from src.api.main import app
from src.rag.admin import AdminError
from src.rag.chunking import chunk_document
from src.rag.db import connect
from src.rag.ingest import sync_documents
from src.rag.loader import load_documents
from src.rag.tenants import get_or_create_tenant
from src.rag.users import set_role_by_email

ADMIN = "/api/v1/admin"
QUERY = "/api/v1/query"
DOCS = "/api/v1/documents"
SUFFIX = secrets.token_hex(4)
QUESTION = "What is the Pelican vault code?"
SECRET = {name: f"{name.upper()}-{secrets.token_hex(3).upper()}" for name in ("company", "vault", "b")}


def ingest(conn, model, tenant_id: int, files: dict[str, str], folder: Path) -> dict[str, int]:
    folder.mkdir(parents=True)
    for name, secret in files.items():
        (folder / name).write_text(f"# Pelican vault ({name})\n\nThe Pelican vault code in {name} is {secret}.\n",
                                   encoding="utf-8")
    documents = load_documents(folder)
    stats = sync_documents(conn, model, documents, {d.relative_path: chunk_document(d) for d in documents},
                           500, 100, tenant_id=tenant_id)
    assert not stats.failed, stats.failed
    return {name: conn.execute("SELECT id FROM documents WHERE tenant_id = %s AND relative_path = %s",
                               (tenant_id, name)).fetchone()[0] for name in files}


class AdminApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.pipeline = app.state.pipeline
        cls.conn = connect()
        register_vector(cls.conn)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tenant = {key: get_or_create_tenant(cls.conn, f"test-admin-{key}-{SUFFIX}", f"Admin test {key}").id
                      for key in ("a", "b")}
        a, b = cls.tenant["a"], cls.tenant["b"]
        cls.doc = ingest(cls.conn, cls.pipeline.embedding_model, a,
                         {"company.md": SECRET["company"], "vault.md": SECRET["vault"]}, Path(cls.tmp.name) / "a")
        cls.doc.update({"b.md": ingest(cls.conn, cls.pipeline.embedding_model, b, {"b.md": SECRET["b"]},
                                       Path(cls.tmp.name) / "b")["b.md"]})
        cls.dept = {}
        for key, tenant_id, slug in (("finance", a, "finance"), ("legal", a, "legal"), ("b_finance", b, "finance")):
            cls.dept[key] = cls.conn.execute("INSERT INTO departments (tenant_id, slug, name) VALUES (%s, %s, %s) RETURNING id",
                                             (tenant_id, slug, slug.title())).fetchone()[0]
        cls.users, cls.emails, cls.tokens = {}, {}, {}
        for name, tenant_id in (("admin", a), ("alice", a), ("bob", a), ("b_admin", b)):
            user, password = make_user(cls.conn, tenant_id, f"admin-test-{name}")
            cls.users[name], cls.emails[name] = user.id, user.email
            cls.tokens[name] = login(cls.client, user.email, password)
        cls.responses = []  # every admin response body, for the secrets check

    @classmethod
    def tearDownClass(cls):
        ids = list(cls.tenant.values())
        cls.conn.execute("DELETE FROM documents WHERE tenant_id = ANY(%s)", (ids,))
        cls.conn.execute("DELETE FROM tenants WHERE id = ANY(%s)", (ids,))  # users, departments, links cascade
        cls.conn.close()
        cls.tmp.cleanup()
        cls.client_context.__exit__(None, None, None)

    def setUp(self):
        """Baseline: admin + b_admin are admins, others employees, no memberships, company documents."""
        ids = list(self.tenant.values())
        self.conn.execute("UPDATE users SET role = CASE WHEN id = ANY(%s) THEN 'admin' ELSE 'employee' END "
                          "WHERE tenant_id = ANY(%s)", ([self.users["admin"], self.users["b_admin"]], ids))
        self.conn.execute("DELETE FROM user_departments WHERE tenant_id = ANY(%s)", (ids,))
        # Employees created by a test through POST /users are removed again.
        self.conn.execute("DELETE FROM users WHERE tenant_id = ANY(%s) AND NOT (id = ANY(%s))",
                          (ids, list(self.users.values())))
        self.conn.execute("DELETE FROM document_departments WHERE tenant_id = ANY(%s)", (ids,))
        self.conn.execute("UPDATE documents SET visibility = 'company' WHERE tenant_id = ANY(%s)", (ids,))
        self.conn.execute("DELETE FROM departments WHERE tenant_id = ANY(%s) AND NOT (id = ANY(%s))",
                          (ids, list(self.dept.values())))
        for key, tenant_key, slug in (("finance", "a", "finance"), ("legal", "a", "legal"), ("b_finance", "b", "finance")):
            # Restored with the same id if a test deleted it; renamed back otherwise.
            self.conn.execute(
                "INSERT INTO departments (id, tenant_id, slug, name) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET slug = EXCLUDED.slug, name = EXCLUDED.name",
                (self.dept[key], self.tenant[tenant_key], slug, slug.title()))

    # --- helpers ---------------------------------------------------------------------------------
    def call(self, user: str, method: str, path: str, body=None):
        response = self.client.request(method, f"{ADMIN}{path}", json=body, headers=bearer(self.tokens[user]))
        self.responses.append(response.text)
        return response

    def retrieved(self, user: str) -> str:
        response = self.client.post(QUERY, headers=bearer(self.tokens[user]),
                                    json={"question": QUESTION, "top_k": 10, "retrieve_only": True})
        self.assertEqual(response.status_code, 200, response.text)
        return "\n".join(s["text"] for s in response.json()["sources"])

    def listed(self, user: str) -> set[int]:
        return {item["id"] for item in self.client.get(DOCS, headers=bearer(self.tokens[user]),
                                                         params={"limit": 200}).json()["items"]}

    def sees_vault(self, user: str) -> bool:
        in_retrieval = SECRET["vault"] in self.retrieved(user)
        in_listing = self.doc["vault.md"] in self.listed(user)
        self.assertEqual(in_retrieval, in_listing, "retrieval and listing disagree")
        return in_retrieval

    def state(self) -> tuple:
        """Everything the admin endpoints can change, for both tenants."""
        ids = list(self.tenant.values())
        q = lambda sql: self.conn.execute(sql, (ids,)).fetchall()
        return (q("SELECT id, role FROM users WHERE tenant_id = ANY(%s) ORDER BY id"),
                q("SELECT id, slug, name FROM departments WHERE tenant_id = ANY(%s) ORDER BY id"),
                q("SELECT user_id, department_id FROM user_departments WHERE tenant_id = ANY(%s) ORDER BY 1, 2"),
                q("SELECT id, visibility FROM documents WHERE tenant_id = ANY(%s) ORDER BY id"),
                q("SELECT document_id, department_id FROM document_departments WHERE tenant_id = ANY(%s) ORDER BY 1, 2"))

    def every_endpoint(self, user_id: int, department_id: int, document_id: int) -> list[tuple]:
        return [
            ("GET", "/departments", None),
            ("POST", "/departments", {"slug": "new-dept", "name": "New"}),
            ("PATCH", f"/departments/{department_id}", {"name": "Renamed"}),
            ("DELETE", f"/departments/{department_id}", None),
            ("GET", "/users", None),
            ("POST", "/users", {"email": f"new-{secrets.token_hex(4)}@example.test", "password": secrets.token_urlsafe(18),
                                "display_name": "New Person", "department_ids": [department_id]}),
            ("PUT", f"/users/{user_id}/role", {"role": "admin"}),
            ("PUT", f"/users/{user_id}/departments/{department_id}", None),
            ("DELETE", f"/users/{user_id}/departments/{department_id}", None),
            ("GET", f"/documents/{document_id}/access", None),
            ("PUT", f"/documents/{document_id}/access", {"visibility": "departments", "department_ids": [department_id]}),
        ]

    # --- admin-only -------------------------------------------------------------------------------
    def test_employees_get_403_on_every_admin_endpoint(self):
        before = self.state()
        for method, path, body in self.every_endpoint(self.users["alice"], self.dept["finance"], self.doc["vault.md"]):
            with self.subTest(endpoint=f"{method} {path}"):
                response = self.call("alice", method, path, body)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "admin_required")
                no_token = self.client.request(method, f"{ADMIN}{path}", json=body)
                self.assertEqual(no_token.status_code, 401)
        self.assertEqual(self.state(), before)

    def test_role_claims_in_a_token_are_ignored(self):
        now = datetime.now(timezone.utc)
        forged = jwt.encode({"sub": str(self.users["alice"]), "typ": "access", "iat": now,
                             "exp": now + timedelta(minutes=5), "role": "admin", "is_admin": True},
                            os.environ["JWT_SECRET_KEY"], algorithm=ALGORITHM)
        response = self.client.get(f"{ADMIN}/departments", headers=bearer(forged))
        self.assertEqual(response.status_code, 403)

    # --- departments -----------------------------------------------------------------------------
    def test_admin_manages_departments(self):
        created = self.call("admin", "POST", "/departments", {"slug": "hr", "name": " Human Resources "})
        self.assertEqual(created.status_code, 201, created.text)
        hr = created.json()
        self.assertEqual((hr["slug"], hr["name"], hr["member_count"], hr["document_count"]),
                         ("hr", "Human Resources", 0, 0))
        self.assertEqual(self.call("admin", "POST", "/departments", {"slug": "hr", "name": "Again"}).json()["error"]["code"],
                         "department_exists")
        for bad in ({"slug": "Bad Slug", "name": "x"}, {"slug": "ok", "name": "  "}, {"slug": "ok", "name": "x", "tenant_id": 1}):
            with self.subTest(body=bad):
                self.assertEqual(self.call("admin", "POST", "/departments", bad).status_code, 422)

        renamed = self.call("admin", "PATCH", f"/departments/{hr['id']}", {"name": "People"})
        self.assertEqual((renamed.status_code, renamed.json()["name"], renamed.json()["slug"]), (200, "People", "hr"))
        clash = self.call("admin", "PATCH", f"/departments/{hr['id']}", {"slug": "finance"})
        self.assertEqual((clash.status_code, clash.json()["error"]["code"]), (409, "department_exists"))

        listed = {d["slug"]: d for d in self.call("admin", "GET", "/departments").json()}
        self.assertEqual(set(listed), {"finance", "legal", "hr"})  # never tenant B's "finance"
        self.assertEqual(listed["finance"]["id"], self.dept["finance"])

        # A department with members but no documents can be deleted; memberships go with it.
        self.call("admin", "PUT", f"/users/{self.users['alice']}/departments/{hr['id']}")
        self.assertEqual(self.call("admin", "DELETE", f"/departments/{hr['id']}").status_code, 204)
        self.assertEqual({d["slug"] for d in self.call("admin", "GET", "/departments").json()}, {"finance", "legal"})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM user_departments WHERE department_id = %s",
                                           (hr["id"],)).fetchone()[0], 0)
        self.assertEqual(self.call("admin", "DELETE", f"/departments/{hr['id']}").status_code, 404)

    def test_department_assigned_to_documents_cannot_be_deleted(self):
        self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access",
                  {"visibility": "departments", "department_ids": [self.dept["finance"]]})
        response = self.call("admin", "DELETE", f"/departments/{self.dept['finance']}")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "department_in_use"))
        self.assertEqual(self.call("admin", "GET", f"/documents/{self.doc['vault.md']}/access").json()["department_ids"],
                         [self.dept["finance"]])

    # --- users ------------------------------------------------------------------------------------
    def test_admin_manages_users_without_seeing_secrets(self):
        body = self.call("admin", "GET", "/users").json()
        self.assertEqual(body["total"], 3)
        self.assertEqual({u["id"] for u in body["items"]}, {self.users[n] for n in ("admin", "alice", "bob")})
        for user in body["items"]:
            self.assertEqual(set(user), {"id", "email", "display_name", "role", "department_ids", "created_at"})
        roles = {u["id"]: u["role"] for u in body["items"]}
        self.assertEqual((roles[self.users["admin"]], roles[self.users["alice"]]), ("admin", "employee"))

        alice = self.users["alice"]
        added = self.call("admin", "PUT", f"/users/{alice}/departments/{self.dept['finance']}")
        self.assertEqual((added.status_code, added.json()["department_ids"]), (200, [self.dept["finance"]]))
        again = self.call("admin", "PUT", f"/users/{alice}/departments/{self.dept['finance']}")  # idempotent
        self.assertEqual(again.json()["department_ids"], [self.dept["finance"]])
        both = self.call("admin", "PUT", f"/users/{alice}/departments/{self.dept['legal']}").json()["department_ids"]
        self.assertEqual(both, sorted([self.dept["finance"], self.dept["legal"]]))
        removed = self.call("admin", "DELETE", f"/users/{alice}/departments/{self.dept['finance']}")
        self.assertEqual(removed.json()["department_ids"], [self.dept["legal"]])

        promoted = self.call("admin", "PUT", f"/users/{alice}/role", {"role": "admin"})
        self.assertEqual((promoted.status_code, promoted.json()["role"]), (200, "admin"))
        self.assertEqual(self.call("admin", "PUT", f"/users/{alice}/role", {"role": "owner"}).status_code, 422)

    def test_the_last_admin_cannot_be_demoted(self):
        response = self.call("b_admin", "PUT", f"/users/{self.users['b_admin']}/role", {"role": "employee"})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (409, "last_admin"))
        # With a second admin, self-demotion works.
        self.call("admin", "PUT", f"/users/{self.users['alice']}/role", {"role": "admin"})
        self.assertEqual(self.call("admin", "PUT", f"/users/{self.users['admin']}/role",
                                   {"role": "employee"}).status_code, 200)
        self.assertEqual(self.call("admin", "GET", "/users").status_code, 403)  # effective immediately

    # --- tenant isolation ---------------------------------------------------------------------------
    def test_cross_tenant_ids_cannot_be_read_or_modified(self):
        before = self.state()
        b_user, b_dept, b_doc = self.users["b_admin"], self.dept["b_finance"], self.doc["b.md"]
        a_user, a_dept, a_doc = self.users["alice"], self.dept["finance"], self.doc["vault.md"]
        attempts = [e for e in self.every_endpoint(b_user, b_dept, b_doc)
                    if e[1] not in ("/departments", "/users")]  # every endpoint that takes an id
        attempts += [
            ("PUT", f"/users/{a_user}/departments/{b_dept}", None),        # own user + foreign department
            ("PUT", f"/users/{b_user}/departments/{a_dept}", None),        # foreign user + own department
            ("PUT", f"/documents/{a_doc}/access", {"visibility": "departments", "department_ids": [b_dept]}),
            ("PUT", f"/documents/{a_doc}/access", {"visibility": "departments", "department_ids": [a_dept, b_dept]}),
        ]
        for method, path, body in attempts:
            with self.subTest(endpoint=f"{method} {path} {body}"):
                response = self.call("admin", method, path, body)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertTrue(response.json()["error"]["code"].endswith("_not_found"))
        # The same 404 as an id that exists nowhere.
        unknown = self.call("admin", "GET", f"/documents/{2**62}/access").json()
        self.assertEqual(self.call("admin", "GET", f"/documents/{b_doc}/access").json(), unknown)
        self.assertEqual(self.state(), before)
        self.assertNotIn(b_user, {u["id"] for u in self.call("admin", "GET", "/users").json()["items"]})

    # --- effects on the next request -------------------------------------------------------------
    def test_membership_changes_apply_to_the_next_request(self):
        self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access",
                  {"visibility": "departments", "department_ids": [self.dept["finance"]]})
        self.assertFalse(self.sees_vault("alice"))
        self.call("admin", "PUT", f"/users/{self.users['alice']}/departments/{self.dept['finance']}")
        self.assertTrue(self.sees_vault("alice"))  # same token, next request
        self.assertFalse(self.sees_vault("bob"))
        self.call("admin", "DELETE", f"/users/{self.users['alice']}/departments/{self.dept['finance']}")
        self.assertFalse(self.sees_vault("alice"))

    def test_restricting_a_document_applies_to_retrieval_and_listing_immediately(self):
        self.assertTrue(self.sees_vault("alice"))
        response = self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access",
                             {"visibility": "departments", "department_ids": [self.dept["legal"]]})
        self.assertEqual(response.json(), {"id": self.doc["vault.md"], "filename": "vault.md", "origin": "folder",
                                           "visibility": "departments", "department_ids": [self.dept["legal"]]})
        self.assertFalse(self.sees_vault("alice"))
        self.assertIn(SECRET["company"], self.retrieved("alice"))  # other documents unaffected
        self.assertTrue(self.sees_vault("admin"))
        self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access", {"visibility": "company"})
        self.assertTrue(self.sees_vault("alice"))
        self.assertEqual(self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access",
                                   {"visibility": "company", "department_ids": [self.dept["legal"]]}).status_code, 422)

    def test_removing_all_departments_makes_a_document_admin_only(self):
        self.call("admin", "PUT", f"/users/{self.users['alice']}/departments/{self.dept['finance']}")
        self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access",
                  {"visibility": "departments", "department_ids": [self.dept["finance"], self.dept["legal"]]})
        self.assertTrue(self.sees_vault("alice"))
        response = self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access",
                             {"visibility": "departments", "department_ids": []})
        self.assertEqual(response.json()["department_ids"], [])
        self.assertFalse(self.sees_vault("alice"))
        self.assertTrue(self.sees_vault("admin"))

    def test_promotion_and_demotion_apply_to_the_next_request(self):
        self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access",
                  {"visibility": "departments", "department_ids": []})
        self.assertEqual(self.call("alice", "GET", "/departments").status_code, 403)
        self.assertFalse(self.sees_vault("alice"))

        self.call("admin", "PUT", f"/users/{self.users['alice']}/role", {"role": "admin"})
        self.assertEqual(self.call("alice", "GET", "/departments").status_code, 200)  # same token
        self.assertTrue(self.sees_vault("alice"))

        self.call("admin", "PUT", f"/users/{self.users['alice']}/role", {"role": "employee"})
        self.assertEqual(self.call("alice", "GET", "/departments").status_code, 403)
        self.assertFalse(self.sees_vault("alice"))

    # --- /auth/me reports the current role and departments (display only) ---------------------------
    def test_me_reports_the_current_role_and_departments(self):
        def me(user: str) -> dict:
            response = self.client.get("/api/v1/auth/me", headers=bearer(self.tokens[user]))
            self.assertEqual(response.status_code, 200)
            self.responses.append(response.text)
            return response.json()

        alice = me("alice")
        self.assertEqual((alice["role"], alice["departments"]), ("employee", []))
        self.assertEqual(set(alice), {"id", "email", "display_name", "tenant", "role", "departments"})
        self.call("admin", "PUT", f"/users/{self.users['alice']}/departments/{self.dept['legal']}")
        self.call("admin", "PUT", f"/users/{self.users['alice']}/departments/{self.dept['finance']}")
        self.call("admin", "PUT", f"/users/{self.users['alice']}/role", {"role": "admin"})
        alice = me("alice")  # same token, next request
        self.assertEqual(alice["role"], "admin")
        self.assertEqual(alice["departments"], [{"id": self.dept["finance"], "slug": "finance", "name": "Finance"},
                                                {"id": self.dept["legal"], "slug": "legal", "name": "Legal"}])
        self.assertEqual(me("b_admin")["departments"], [])  # never another tenant's departments

    # --- operator bootstrap -----------------------------------------------------------------------
    def test_operator_can_promote_an_existing_user(self):
        user = set_role_by_email(self.conn, self.emails["bob"].upper(), "admin")
        self.assertEqual(user.id, self.users["bob"])
        self.assertEqual(self.call("bob", "GET", "/users").status_code, 200)
        with self.assertRaises(AdminError):  # the same "keep one admin" rule applies
            set_role_by_email(self.conn, self.emails["b_admin"], "employee")
        with self.assertRaises(LookupError):
            set_role_by_email(self.conn, "nobody-" + self.emails["bob"], "admin")

    # --- creating employees -------------------------------------------------------------------------
    def new_employee(self, admin: str = "admin", record: bool = True, **overrides):
        body = {"email": f"emp-{secrets.token_hex(4)}@example.test", "password": secrets.token_urlsafe(18),
                "display_name": "  Ravi Kumar ", "department_ids": [], **overrides}
        if record:
            return self.call(admin, "POST", "/users", body), body
        # Not kept for the secrets check: 422 validation messages name the "password" field.
        return self.client.post(f"{ADMIN}/users", json=body, headers=bearer(self.tokens[admin])), body

    def test_admin_creates_an_employee_in_a_department_who_can_log_in(self):
        self.call("admin", "PUT", f"/documents/{self.doc['vault.md']}/access",
                  {"visibility": "departments", "department_ids": [self.dept["finance"]]})
        response, body = self.new_employee(department_ids=[self.dept["finance"]])
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        self.assertEqual((created["email"], created["display_name"], created["role"], created["department_ids"]),
                         (body["email"], "Ravi Kumar", "employee", [self.dept["finance"]]))
        self.assertNotIn("password", response.text.lower())
        self.assertIn(created["id"], [u["id"] for u in self.call("admin", "GET", "/users?limit=200").json()["items"]])

        # The new employee logs in with the initial password and gets exactly Finance access.
        token = login(self.client, body["email"], body["password"])
        me = self.client.get("/api/v1/auth/me", headers=bearer(token)).json()
        self.assertEqual((me["role"], [d["name"] for d in me["departments"]]), ("employee", ["Finance"]))
        listed = {i["id"] for i in self.client.get(DOCS, headers=bearer(token), params={"limit": 200}).json()["items"]}
        self.assertIn(self.doc["vault.md"], listed)
        self.assertNotIn(self.doc["vault.md"], self.listed("alice"))  # alice is in no department
        self.assertEqual(self.client.get(f"{ADMIN}/users", headers=bearer(token)).status_code, 403)

    def test_creating_employees_is_validated_and_all_or_nothing(self):
        users_before = self.state()[0]
        existing, _ = self.new_employee(email=self.emails["alice"])
        self.assertEqual((existing.status_code, existing.json()["error"]["code"]), (409, "email_already_registered"))
        # A department of another tenant looks like an unknown one, and nothing is created.
        foreign, body = self.new_employee(department_ids=[self.dept["finance"], self.dept["b_finance"]])
        self.assertEqual((foreign.status_code, foreign.json()["error"]["code"]), (404, "department_not_found"))
        self.assertIsNone(self.conn.execute("SELECT id FROM users WHERE email = %s", (body["email"],)).fetchone())
        for overrides in ({"password": "short"}, {"email": "not-an-email"}, {"role": "admin"},
                          {"tenant_id": self.tenant["b"]}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.new_employee(record=False, **overrides)[0].status_code, 422)
        self.assertEqual(self.state()[0], users_before)

    def test_new_employees_always_belong_to_the_admins_own_tenant(self):
        response, body = self.new_employee(admin="b_admin")
        self.assertEqual(response.status_code, 201, response.text)
        tenant = self.conn.execute("SELECT tenant_id FROM users WHERE id = %s", (response.json()["id"],)).fetchone()[0]
        self.assertEqual(tenant, self.tenant["b"])
        self.assertNotIn(body["email"], [u["email"] for u in self.call("admin", "GET", "/users?limit=200").json()["items"]])

    # --- deleting users -----------------------------------------------------------------------------
    def test_admin_deletes_an_employee_whose_access_ends_immediately(self):
        response, body = self.new_employee(department_ids=[self.dept["finance"]])
        employee = response.json()["id"]
        token = login(self.client, body["email"], body["password"])
        self.assertEqual(self.client.get(DOCS, headers=bearer(token)).status_code, 200)

        deleted = self.call("admin", "DELETE", f"/users/{employee}")
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertNotIn(employee, [u["id"] for u in self.call("admin", "GET", "/users?limit=200").json()["items"]])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM user_departments WHERE user_id = %s",
                                           (employee,)).fetchone()[0], 0)
        self.assertEqual(self.client.get(DOCS, headers=bearer(token)).status_code, 401)  # old token is dead
        self.assertEqual(self.call("admin", "DELETE", f"/users/{employee}").status_code, 404)  # already gone

    def test_delete_refuses_self_other_tenants_and_employees(self):
        before = self.state()
        own = self.call("admin", "DELETE", f"/users/{self.users['admin']}")
        self.assertEqual((own.status_code, own.json()["error"]["code"]), (409, "cannot_delete_self"))
        foreign = self.call("admin", "DELETE", f"/users/{self.users['b_admin']}")
        self.assertEqual((foreign.status_code, foreign.json()["error"]["code"]), (404, "user_not_found"))
        employee = self.call("alice", "DELETE", f"/users/{self.users['bob']}")
        self.assertEqual((employee.status_code, employee.json()["error"]["code"]), (403, "admin_required"))
        self.assertEqual(self.client.delete(f"{ADMIN}/users/{self.users['bob']}").status_code, 401)
        self.assertEqual(self.state(), before)

    def test_an_admin_can_delete_another_admin(self):
        self.call("admin", "PUT", f"/users/{self.users['bob']}/role", {"role": "admin"})
        response, _ = self.new_employee()
        other_admin = response.json()["id"]
        self.call("admin", "PUT", f"/users/{other_admin}/role", {"role": "admin"})
        self.assertEqual(self.call("admin", "DELETE", f"/users/{other_admin}").status_code, 204)

    # --- no secrets ---------------------------------------------------------------------------------
    def test_responses_never_contain_password_hashes_or_secrets(self):
        # Exercise every endpoint once as an admin, then inspect everything returned so far.
        for method, path, body in self.every_endpoint(self.users["bob"], self.dept["legal"], self.doc["vault.md"]):
            self.call("admin", method, path, body)
        hashes = [r[0] for r in self.conn.execute("SELECT password_hash FROM users WHERE tenant_id = ANY(%s)",
                                                  (list(self.tenant.values()),))]
        text = "\n".join(self.responses).lower()
        for needle in ("password", "$argon2", "hash", os.environ["JWT_SECRET_KEY"].lower(), *[h.lower() for h in hashes]):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
