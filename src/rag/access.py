"""Who may read which documents: the AccessScope and its SQL filter.

An AccessScope is built on the server for every request, from the database
(users.role and user_departments), never from token claims or request data.
Retrieval applies it inside SQL, in the same WHERE clause as the tenant filter,
so unauthorized chunks are never selected, ranked, reranked or returned.

Rules (within the scope's tenant only):
    admin     -> every document
    employee  -> documents with visibility 'company'
              -> documents with visibility 'departments' assigned to at least
                 one of the employee's departments
A 'departments' document with no departments assigned is visible to admins only.
"""

from dataclasses import dataclass

import psycopg

ADMIN = "admin"
EMPLOYEE = "employee"


@dataclass(frozen=True)
class AccessScope:
    tenant_id: int
    user_id: int | None  # None for operator tools (CLI), which act as the tenant's admin
    role: str
    department_ids: tuple[int, ...] = ()

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    @classmethod
    def operator(cls, tenant_id: int) -> "AccessScope":
        """Full access to one tenant, for server-side operator tools (ingestion, CLI search, evaluation)."""
        return cls(tenant_id=tenant_id, user_id=None, role=ADMIN)

    def sql_params(self) -> dict:
        """Bind parameters used by DOCUMENT_ACCESS_FILTER."""
        return {
            "tenant_id": self.tenant_id,
            "is_admin": self.is_admin,
            "department_ids": list(self.department_ids),
        }


def load_access_scope(conn: psycopg.Connection, user_id: int) -> AccessScope | None:
    """The user's current scope from the database, or None if the user does not exist."""
    row = conn.execute(
        """
        SELECT u.tenant_id, u.role,
               coalesce(array_agg(ud.department_id ORDER BY ud.department_id)
                        FILTER (WHERE ud.department_id IS NOT NULL), '{}')
        FROM users u
        LEFT JOIN user_departments ud ON ud.user_id = u.id AND ud.tenant_id = u.tenant_id
        WHERE u.id = %s
        GROUP BY u.id
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    tenant_id, role, department_ids = row
    return AccessScope(tenant_id=tenant_id, user_id=user_id, role=role, department_ids=tuple(department_ids))


# Which documents (alias d) the scope may read. Used with AccessScope.sql_params().
# Only the literal 'company' opens a document to everyone; any other visibility
# requires a matching department assignment, so unknown values fail closed.
DOCUMENT_ACCESS_FILTER = """
    d.tenant_id = %(tenant_id)s
    AND (
        %(is_admin)s
        OR d.visibility = 'company'
        OR EXISTS (
            SELECT 1 FROM document_departments dd
            WHERE dd.document_id = d.id
              AND dd.tenant_id = %(tenant_id)s
              AND dd.department_id = ANY(%(department_ids)s::bigint[])
        )
    )
"""
