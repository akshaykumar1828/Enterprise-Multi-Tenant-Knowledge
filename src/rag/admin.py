"""Company administration: departments, user roles and memberships, document access.

Every function takes a trusted tenant_id (the admin's AccessScope.tenant_id, loaded
from the database) and filters every statement by it; ids from another tenant
behave exactly like ids that do not exist (404). Link rows carry tenant_id, so the
schema's composite foreign keys also reject any cross-tenant link.

This module only edits the existing authorization data (users.role,
user_departments, documents.visibility, document_departments); who may read what
is still decided solely by access.DOCUMENT_ACCESS_FILTER.
"""

from dataclasses import dataclass
from datetime import datetime

import psycopg

from .access import ADMIN, EMPLOYEE

ROLES = (ADMIN, EMPLOYEE)
COMPANY, DEPARTMENTS = "company", "departments"
VISIBILITIES = (COMPANY, DEPARTMENTS)


class AdminError(Exception):
    """A rejected admin operation, with the HTTP status the API should use."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_found(kind: str) -> AdminError:
    # Same answer whether the row does not exist or belongs to another tenant.
    return AdminError(404, f"{kind}_not_found", f"{kind.capitalize()} not found.")


# --- departments --------------------------------------------------------------------

@dataclass(frozen=True)
class Department:
    id: int
    slug: str
    name: str
    member_count: int
    document_count: int


_DEPARTMENT_SELECT = """
    SELECT d.id, d.slug, d.name,
           (SELECT count(*) FROM user_departments ud WHERE ud.department_id = d.id AND ud.tenant_id = d.tenant_id),
           (SELECT count(*) FROM document_departments dd WHERE dd.department_id = d.id AND dd.tenant_id = d.tenant_id)
    FROM departments d
"""


def list_departments(conn: psycopg.Connection, tenant_id: int) -> list[Department]:
    rows = conn.execute(f"{_DEPARTMENT_SELECT} WHERE d.tenant_id = %s ORDER BY d.name, d.id", (tenant_id,)).fetchall()
    return [Department(*row) for row in rows]


def get_department(conn: psycopg.Connection, tenant_id: int, department_id: int) -> Department:
    row = conn.execute(f"{_DEPARTMENT_SELECT} WHERE d.tenant_id = %s AND d.id = %s", (tenant_id, department_id)).fetchone()
    if row is None:
        raise not_found("department")
    return Department(*row)


def _slug_taken() -> AdminError:
    return AdminError(409, "department_exists", "A department with this slug already exists.")


def create_department(conn: psycopg.Connection, tenant_id: int, slug: str, name: str) -> Department:
    try:
        with conn.transaction():
            department_id = conn.execute(
                "INSERT INTO departments (tenant_id, slug, name) VALUES (%s, %s, %s) RETURNING id",
                (tenant_id, slug, name)).fetchone()[0]
    except psycopg.errors.UniqueViolation:
        raise _slug_taken() from None
    return get_department(conn, tenant_id, department_id)


def update_department(
    conn: psycopg.Connection, tenant_id: int, department_id: int, slug: str | None, name: str | None
) -> Department:
    try:
        with conn.transaction():
            updated = conn.execute(
                "UPDATE departments SET slug = coalesce(%s, slug), name = coalesce(%s, name) "
                "WHERE tenant_id = %s AND id = %s RETURNING id",
                (slug, name, tenant_id, department_id)).fetchone()
    except psycopg.errors.UniqueViolation:
        raise _slug_taken() from None
    if updated is None:
        raise not_found("department")
    return get_department(conn, tenant_id, department_id)


def delete_department(conn: psycopg.Connection, tenant_id: int, department_id: int) -> None:
    """Delete a department that no document is assigned to; its memberships go with it.

    The foreign keys would cascade document assignments away too, silently
    changing who can read those documents, so that case is refused: the admin
    must first change those documents' access explicitly.
    """
    with conn.transaction():
        # Row lock: a concurrent assignment (which needs this row) waits until we finish.
        if conn.execute("SELECT id FROM departments WHERE tenant_id = %s AND id = %s FOR UPDATE",
                        (tenant_id, department_id)).fetchone() is None:
            raise not_found("department")
        assigned = conn.execute("SELECT count(*) FROM document_departments WHERE tenant_id = %s AND department_id = %s",
                                (tenant_id, department_id)).fetchone()[0]
        if assigned:
            raise AdminError(409, "department_in_use",
                             f"This department is assigned to {assigned} document(s). "
                             "Change those documents' access first.")
        conn.execute("DELETE FROM departments WHERE tenant_id = %s AND id = %s", (tenant_id, department_id))


# --- users --------------------------------------------------------------------------

@dataclass(frozen=True)
class CompanyUser:
    """A user as an admin sees it. There is deliberately no password hash here."""

    id: int
    email: str
    display_name: str | None
    role: str
    department_ids: tuple[int, ...]
    created_at: datetime


_USER_SELECT = """
    SELECT u.id, u.email, u.display_name, u.role,
           coalesce(array_agg(ud.department_id ORDER BY ud.department_id)
                    FILTER (WHERE ud.department_id IS NOT NULL), '{}'),
           u.created_at
    FROM users u
    LEFT JOIN user_departments ud ON ud.user_id = u.id AND ud.tenant_id = u.tenant_id
"""


def _company_user(row) -> CompanyUser:
    return CompanyUser(*row[:4], tuple(row[4]), row[5])


def list_users(conn: psycopg.Connection, tenant_id: int, limit: int, offset: int) -> tuple[int, list[CompanyUser]]:
    total = conn.execute("SELECT count(*) FROM users WHERE tenant_id = %s", (tenant_id,)).fetchone()[0]
    rows = conn.execute(f"{_USER_SELECT} WHERE u.tenant_id = %s GROUP BY u.id ORDER BY u.id LIMIT %s OFFSET %s",
                        (tenant_id, limit, offset)).fetchall()
    return total, [_company_user(row) for row in rows]


def get_company_user(conn: psycopg.Connection, tenant_id: int, user_id: int) -> CompanyUser:
    row = conn.execute(f"{_USER_SELECT} WHERE u.tenant_id = %s AND u.id = %s GROUP BY u.id",
                       (tenant_id, user_id)).fetchone()
    if row is None:
        raise not_found("user")
    return _company_user(row)


def set_user_role(conn: psycopg.Connection, tenant_id: int, user_id: int, role: str) -> None:
    """Make a user admin or employee. A company always keeps at least one admin."""
    if role not in ROLES:
        raise AdminError(422, "invalid_role", "Role must be 'admin' or 'employee'.")
    with conn.transaction():
        # Lock the company's admins first, so two concurrent demotions cannot both
        # see "another admin remains" and leave the company without one.
        admins = [r[0] for r in conn.execute(
            "SELECT id FROM users WHERE tenant_id = %s AND role = %s ORDER BY id FOR UPDATE", (tenant_id, ADMIN))]
        current = conn.execute("SELECT role FROM users WHERE tenant_id = %s AND id = %s FOR UPDATE",
                               (tenant_id, user_id)).fetchone()
        if current is None:
            raise not_found("user")
        if current[0] == ADMIN and role != ADMIN and admins == [user_id]:
            raise AdminError(409, "last_admin", "The company must keep at least one admin.")
        conn.execute("UPDATE users SET role = %s WHERE tenant_id = %s AND id = %s", (role, tenant_id, user_id))


def _require_user_and_department(conn: psycopg.Connection, tenant_id: int, user_id: int, department_id: int) -> None:
    if conn.execute("SELECT 1 FROM users WHERE tenant_id = %s AND id = %s", (tenant_id, user_id)).fetchone() is None:
        raise not_found("user")
    if conn.execute("SELECT 1 FROM departments WHERE tenant_id = %s AND id = %s",
                    (tenant_id, department_id)).fetchone() is None:
        raise not_found("department")


def add_membership(conn: psycopg.Connection, tenant_id: int, user_id: int, department_id: int) -> None:
    """Idempotent. The composite foreign keys also reject any cross-tenant pair."""
    _require_user_and_department(conn, tenant_id, user_id, department_id)
    conn.execute("INSERT INTO user_departments (tenant_id, user_id, department_id) VALUES (%s, %s, %s) "
                 "ON CONFLICT DO NOTHING", (tenant_id, user_id, department_id))


def remove_membership(conn: psycopg.Connection, tenant_id: int, user_id: int, department_id: int) -> None:
    """Idempotent."""
    _require_user_and_department(conn, tenant_id, user_id, department_id)
    conn.execute("DELETE FROM user_departments WHERE tenant_id = %s AND user_id = %s AND department_id = %s",
                 (tenant_id, user_id, department_id))


# --- document access ---------------------------------------------------------------------

@dataclass(frozen=True)
class DocumentAccess:
    id: int
    filename: str
    origin: str
    visibility: str
    department_ids: tuple[int, ...]


def get_document_access(conn: psycopg.Connection, tenant_id: int, document_id: int) -> DocumentAccess:
    row = conn.execute(
        """SELECT d.id, d.source, d.origin, d.visibility,
                  coalesce(array_agg(dd.department_id ORDER BY dd.department_id)
                           FILTER (WHERE dd.department_id IS NOT NULL), '{}')
           FROM documents d
           LEFT JOIN document_departments dd ON dd.document_id = d.id AND dd.tenant_id = d.tenant_id
           WHERE d.tenant_id = %s AND d.id = %s
           GROUP BY d.id""", (tenant_id, document_id)).fetchone()
    if row is None:
        raise not_found("document")
    return DocumentAccess(*row[:4], tuple(row[4]))


def set_document_access(
    conn: psycopg.Connection, tenant_id: int, document_id: int, visibility: str, department_ids: list[int]
) -> DocumentAccess:
    """Replace a document's visibility and department assignments in one transaction.

    'company' clears the assignments. 'departments' with an empty list leaves the
    document readable by admins only (see access.py). Only the document row's
    visibility and its assignment rows change; chunks and files are untouched.
    """
    if visibility not in VISIBILITIES:
        raise AdminError(422, "invalid_visibility", "Visibility must be 'company' or 'departments'.")
    wanted = sorted(set(department_ids)) if visibility == DEPARTMENTS else []
    with conn.transaction():
        if conn.execute("SELECT id FROM documents WHERE tenant_id = %s AND id = %s FOR UPDATE",
                        (tenant_id, document_id)).fetchone() is None:
            raise not_found("document")
        found = [r[0] for r in conn.execute(
            "SELECT id FROM departments WHERE tenant_id = %s AND id = ANY(%s::bigint[]) ORDER BY id",
            (tenant_id, wanted))]
        if found != wanted:
            raise not_found("department")
        conn.execute("UPDATE documents SET visibility = %s WHERE tenant_id = %s AND id = %s",
                     (visibility, tenant_id, document_id))
        conn.execute("DELETE FROM document_departments WHERE tenant_id = %s AND document_id = %s "
                     "AND NOT (department_id = ANY(%s::bigint[]))", (tenant_id, document_id, wanted))
        conn.execute("INSERT INTO document_departments (tenant_id, document_id, department_id) "
                     "SELECT %s, %s, unnest(%s::bigint[]) ON CONFLICT DO NOTHING", (tenant_id, document_id, wanted))
    return get_document_access(conn, tenant_id, document_id)
