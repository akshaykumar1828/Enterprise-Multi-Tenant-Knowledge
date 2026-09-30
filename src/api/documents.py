"""Knowledge-base routes: upload, list and delete the caller's tenant documents.

All three require authentication and act only on the authenticated user's
tenant; no tenant, path or storage location is ever taken from the request.
Listing and deleting are further limited to documents the user may read
(AccessScope); anything else looks exactly like a document that does not exist.
"""

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from pgvector.psycopg import register_vector

from src.rag.db import connect_app
from src.rag.uploads import delete_document, ingest_upload, list_documents, max_upload_bytes
from .auth import AuthenticatedUser, get_current_user
from .rate_limit import enforce
from .schemas import DocumentListResponse, DocumentResponse, ErrorResponse

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
ERRORS = {code: {"model": ErrorResponse} for code in (401, 404, 409, 413, 415, 422, 429, 500)}


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED, responses=ERRORS)
def upload_document(
    request: Request,
    file: UploadFile = File(description="A PDF, TXT or Markdown file."),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentResponse:
    enforce("UPLOADS", f"upload:tenant:{user.scope.tenant_id}",
            "Your organization has uploaded too many documents recently. Please try again later.")
    # Read at most one byte past the limit: enough to tell "too large" without loading more.
    data = file.file.read(max_upload_bytes() + 1)
    pipeline = request.app.state.pipeline
    with connect_app() as conn:
        register_vector(conn)
        # Tenant, role and departments come from the database (user.scope), never from the request.
        info = ingest_upload(conn, pipeline.embedding_model, pipeline.model_lock, user.scope,
                             file.filename or "", data)
    return DocumentResponse.from_info(info)


@router.get("", response_model=DocumentListResponse, responses=ERRORS)
def list_tenant_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentListResponse:
    # Only documents the user may read (role and departments from the database).
    with connect_app() as conn:
        total, items = list_documents(conn, user.scope, limit, offset)
    return DocumentListResponse(total=total, limit=limit, offset=offset,
                                items=[DocumentResponse.from_info(item) for item in items])


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
def delete_tenant_document(document_id: int, user: AuthenticatedUser = Depends(get_current_user)) -> Response:
    with connect_app() as conn:
        delete_document(conn, user.scope, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
