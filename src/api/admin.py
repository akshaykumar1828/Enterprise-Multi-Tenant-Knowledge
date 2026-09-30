"""Admin routes: departments, user roles and memberships, document access.

Every route requires an admin, decided only by the AccessScope that
get_current_user loads from the database on each request (never by token
claims or request data). Every operation is limited to the admin's own tenant
(scope.tenant_id); ids from another tenant get the same 404 as unknown ids.
Changes apply to the affected users' very next request, because every request
reloads its scope.
"""

from fastapi import APIRouter, Depends, Query, Response, status

from src.rag import admin as service
from src.rag.db import connect_app

from .auth import AuthenticatedUser, get_current_user
from .schemas import (
    CompanyUserListResponse,
    CompanyUserResponse,
    DepartmentCreateRequest,
    DepartmentResponse,
    DepartmentUpdateRequest,
    DocumentAccessRequest,
    DocumentAccessResponse,
    ErrorResponse,
    RoleRequest,
)


class AdminRequired(Exception):
    """Turned into 403 by the app's exception handler."""


def require_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if not user.scope.is_admin:
        raise AdminRequired()
    return user


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
ERRORS = {code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422)}


def _user_response(user: service.CompanyUser) -> CompanyUserResponse:
    return CompanyUserResponse(id=user.id, email=user.email, display_name=user.display_name, role=user.role,
                               department_ids=list(user.department_ids), created_at=user.created_at)


def _document_response(access: service.DocumentAccess) -> DocumentAccessResponse:
    return DocumentAccessResponse(id=access.id, filename=access.filename, origin=access.origin,
                                  visibility=access.visibility, department_ids=list(access.department_ids))


# --- departments ------------------------------------------------------------------------

@router.get("/departments", response_model=list[DepartmentResponse], responses=ERRORS)
def list_departments(admin: AuthenticatedUser = Depends(require_admin)) -> list[DepartmentResponse]:
    with connect_app() as conn:
        departments = service.list_departments(conn, admin.scope.tenant_id)
    return [DepartmentResponse(**vars(d)) for d in departments]


@router.post("/departments", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED, responses=ERRORS)
def create_department(body: DepartmentCreateRequest, admin: AuthenticatedUser = Depends(require_admin)) -> DepartmentResponse:
    with connect_app() as conn:
        department = service.create_department(conn, admin.scope.tenant_id, body.slug, body.name)
    return DepartmentResponse(**vars(department))


@router.patch("/departments/{department_id}", response_model=DepartmentResponse, responses=ERRORS)
def update_department(
    department_id: int, body: DepartmentUpdateRequest, admin: AuthenticatedUser = Depends(require_admin)
) -> DepartmentResponse:
    with connect_app() as conn:
        department = service.update_department(conn, admin.scope.tenant_id, department_id, body.slug, body.name)
    return DepartmentResponse(**vars(department))


@router.delete("/departments/{department_id}", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
def delete_department(department_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> Response:
    with connect_app() as conn:
        service.delete_department(conn, admin.scope.tenant_id, department_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- users ------------------------------------------------------------------------------

@router.get("/users", response_model=CompanyUserListResponse, responses=ERRORS)
def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: AuthenticatedUser = Depends(require_admin),
) -> CompanyUserListResponse:
    with connect_app() as conn:
        total, users = service.list_users(conn, admin.scope.tenant_id, limit, offset)
    return CompanyUserListResponse(total=total, limit=limit, offset=offset, items=[_user_response(u) for u in users])


@router.put("/users/{user_id}/role", response_model=CompanyUserResponse, responses=ERRORS)
def set_user_role(user_id: int, body: RoleRequest, admin: AuthenticatedUser = Depends(require_admin)) -> CompanyUserResponse:
    with connect_app() as conn:
        service.set_user_role(conn, admin.scope.tenant_id, user_id, body.role)
        return _user_response(service.get_company_user(conn, admin.scope.tenant_id, user_id))


@router.put("/users/{user_id}/departments/{department_id}", response_model=CompanyUserResponse, responses=ERRORS)
def add_membership(
    user_id: int, department_id: int, admin: AuthenticatedUser = Depends(require_admin)
) -> CompanyUserResponse:
    with connect_app() as conn:
        service.add_membership(conn, admin.scope.tenant_id, user_id, department_id)
        return _user_response(service.get_company_user(conn, admin.scope.tenant_id, user_id))


@router.delete("/users/{user_id}/departments/{department_id}", response_model=CompanyUserResponse, responses=ERRORS)
def remove_membership(
    user_id: int, department_id: int, admin: AuthenticatedUser = Depends(require_admin)
) -> CompanyUserResponse:
    with connect_app() as conn:
        service.remove_membership(conn, admin.scope.tenant_id, user_id, department_id)
        return _user_response(service.get_company_user(conn, admin.scope.tenant_id, user_id))


# --- document access ---------------------------------------------------------------------

@router.get("/documents/{document_id}/access", response_model=DocumentAccessResponse, responses=ERRORS)
def get_document_access(document_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> DocumentAccessResponse:
    with connect_app() as conn:
        return _document_response(service.get_document_access(conn, admin.scope.tenant_id, document_id))


@router.put("/documents/{document_id}/access", response_model=DocumentAccessResponse, responses=ERRORS)
def set_document_access(
    document_id: int, body: DocumentAccessRequest, admin: AuthenticatedUser = Depends(require_admin)
) -> DocumentAccessResponse:
    with connect_app() as conn:
        access = service.set_document_access(conn, admin.scope.tenant_id, document_id,
                                             body.visibility, body.department_ids)
    return _document_response(access)
