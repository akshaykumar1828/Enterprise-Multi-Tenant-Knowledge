"""FastAPI app.

Routes:
    POST /api/v1/auth/register   public: new organization (tenant) + first user
    POST /api/v1/auth/login      public: email + password -> access token
    GET  /api/v1/auth/me         authenticated
    POST /api/v1/query           authenticated: searches only the caller's tenant
    POST /api/v1/documents       authenticated: upload PDF/TXT/Markdown into the caller's tenant
    GET  /api/v1/documents       authenticated: list the caller's tenant documents
    DELETE /api/v1/documents/{id} authenticated: delete one of the caller's uploaded documents
    GET  /api/v1/health          public

Start (from the project root):
    .venv\\Scripts\\python.exe -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000

The heavy work lives in src.rag.pipeline.RAGPipeline; this module only maps
HTTP requests to it and pipeline errors to HTTP responses.
"""

import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from google.genai import errors as genai_errors
from psycopg_pool import PoolTimeout

from src.rag.admin import AdminError
from src.rag.db import close_app_pool, connect_app, open_app_pool, pool_settings
from src.rag.llm import LLMError
from src.rag.pipeline import CorpusEmpty, GenerationUnavailable, RAGPipeline
from src.rag.uploads import UploadError, max_upload_bytes
from src.rag.users import EmailAlreadyRegistered, InvalidCredentials

from .admin import AdminRequired
from .admin import router as admin_router
from .auth import AuthenticatedUser, AuthError, RegistrationClosed, get_current_user, secret_key
from .auth import router as auth_router
from .documents import router as documents_router
from .logging_setup import configure_logging, log_requests_enabled, request_id_from, request_id_var
from .rate_limit import RateLimited, enforce
from .settings import (
    API_DOC_PATHS,
    check_production_database,
    is_production,
    validate_limit_settings,
    validate_production_settings,
)
from .schemas import (
    Citation,
    DatabaseStatus,
    ErrorResponse,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    Source,
    Timings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("rag.api")
request_log = logging.getLogger("rag.api.request")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load configuration and models once; every request reuses them.
    load_dotenv(PROJECT_ROOT / ".env")
    configure_logging()
    secret_key()  # fail at startup, not on the first login, if JWT_SECRET_KEY is missing
    validate_limit_settings()  # and on malformed limit settings
    pool_settings()  # and on malformed database pool/timeout settings
    if is_production():
        # Configuration problems are reported before anything connects to the database.
        for warning in validate_production_settings():
            log.warning(warning)
    open_app_pool()
    try:
        if is_production():
            # Then the database identity (least privilege), before loading models.
            check_production_database(connect_app)
            log.info("starting in production mode (API docs disabled, minimal health output)")
        app.state.pipeline = RAGPipeline()
        yield
    finally:
        close_app_pool()


app = FastAPI(title="Enterprise Knowledge RAG API", version="0.3.0", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(admin_router)

# Room for the multipart envelope (boundaries, headers) around the file itself.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


@app.middleware("http")
async def hide_api_docs_in_production(request: Request, call_next):
    # FastAPI registers the docs routes at import time; in production they are
    # answered with a plain 404 instead (decided per request, from APP_ENV).
    if is_production() and request.url.path in API_DOC_PATHS:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    return await call_next(request)


@app.middleware("http")
async def reject_oversized_uploads(request: Request, call_next):
    # Refuse an upload whose declared size is already too large before its body
    # is read, so it cannot fill the disk while being received. (The route
    # still checks the actual file size.)
    if request.method == "POST" and request.url.path == "/api/v1/documents":
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_upload_bytes() + MULTIPART_OVERHEAD_BYTES:
            return error_response(413, "file_too_large", f"Files can be at most {max_upload_bytes() // (1024 * 1024)} MB.")
    return await call_next(request)


@app.middleware("http")
async def request_context(request: Request, call_next):
    # Outermost middleware (registered last): every response, including ones produced
    # by the middlewares above, gets an X-Request-ID, and every log line written while
    # handling the request carries it.
    request_id = request_id_from(request.headers.get("x-request-id"))
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception:
            # Unexpected errors: full traceback in the log, a generic body for the client.
            log.exception("unhandled error")
            response = JSONResponse(status_code=500, content={"error": {
                "code": "internal_error", "message": "An unexpected error occurred.", "request_id": request_id}})
        response.headers["X-Request-ID"] = request_id
        if log_requests_enabled():
            request_log.info("request", extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,  # never the query string
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "client_ip": request.client.host if request.client else "-",
            })
        return response
    finally:
        request_id_var.reset(token)


def get_pipeline(request: Request) -> RAGPipeline:
    return request.app.state.pipeline


# --- error handling ----------------------------------------------------------

def error_response(status: int, code: str, message: str, retry_after: float | None = None) -> JSONResponse:
    body = ErrorResponse(error={"code": code, "message": message, "retry_after_seconds": retry_after})
    headers = {"Retry-After": str(int(retry_after + 1))} if retry_after is not None else None
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


@app.exception_handler(genai_errors.APIError)
def gemini_error(request: Request, error: genai_errors.APIError) -> JSONResponse:
    if error.code == 429:
        match = re.search(r"retry in ([\d.]+)s", error.message or "")
        return error_response(
            429,
            "llm_quota_exceeded",
            "The Gemini free-tier quota is exhausted. Try again later, or use retrieve_only.",
            float(match.group(1)) if match else None,
        )
    log.warning("Gemini API error %s: %s", error.code, error.message)
    return error_response(502, "llm_error", f"Gemini API request failed ({error.code}).")


@app.exception_handler(LLMError)
def llm_error(request: Request, error: LLMError) -> JSONResponse:
    return error_response(502, "llm_error", str(error))


@app.exception_handler(GenerationUnavailable)
def generation_unavailable(request: Request, error: GenerationUnavailable) -> JSONResponse:
    return error_response(503, "llm_not_configured", str(error))


@app.exception_handler(AuthError)
def auth_error(request: Request, error: AuthError) -> JSONResponse:
    response = error_response(401, error.code, error.message)
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


@app.exception_handler(RateLimited)
def rate_limited(request: Request, error: RateLimited) -> JSONResponse:
    return error_response(429, error.code, error.message, retry_after=error.retry_after_seconds)


@app.exception_handler(RegistrationClosed)
def registration_closed(request: Request, error: RegistrationClosed) -> JSONResponse:
    return error_response(403, "registration_closed",
                          "Self-service registration is closed. Ask an administrator to create your account.")


@app.exception_handler(UploadError)
def upload_error(request: Request, error: UploadError) -> JSONResponse:
    return error_response(error.status, error.code, error.message)


@app.exception_handler(AdminRequired)
def admin_required(request: Request, error: AdminRequired) -> JSONResponse:
    return error_response(403, "admin_required", "This action requires an administrator.")


@app.exception_handler(AdminError)
def admin_error(request: Request, error: AdminError) -> JSONResponse:
    return error_response(error.status, error.code, error.message)


@app.exception_handler(InvalidCredentials)
def invalid_credentials(request: Request, error: InvalidCredentials) -> JSONResponse:
    # Same response for an unknown email and a wrong password.
    return error_response(401, "invalid_credentials", "Incorrect email or password.")


@app.exception_handler(EmailAlreadyRegistered)
def email_already_registered(request: Request, error: EmailAlreadyRegistered) -> JSONResponse:
    return error_response(409, "email_already_registered", "An account with this email already exists.")


@app.exception_handler(CorpusEmpty)
def corpus_empty(request: Request, error: CorpusEmpty) -> JSONResponse:
    return error_response(503, "corpus_empty", str(error))


@app.exception_handler(PoolTimeout)
def database_busy(request: Request, error: PoolTimeout) -> JSONResponse:
    log.warning("no database connection available within the pool timeout")
    return error_response(503, "database_busy", "The service is busy. Please try again shortly.", retry_after=2)


@app.exception_handler(psycopg.errors.QueryCanceled)
def database_timeout(request: Request, error: psycopg.errors.QueryCanceled) -> JSONResponse:
    # Raised by the per-connection statement_timeout (more specific than OperationalError).
    log.warning("database statement timed out")
    return error_response(503, "database_timeout", "The request took too long. Please try again.")


@app.exception_handler(psycopg.OperationalError)
def database_unavailable(request: Request, error: psycopg.OperationalError) -> JSONResponse:
    log.warning("Database unavailable: %s", error)
    return error_response(503, "database_unavailable", "Could not connect to PostgreSQL.")


# --- routes ------------------------------------------------------------------

ERROR_RESPONSES = {code: {"model": ErrorResponse} for code in (401, 429, 502, 503)}


@app.post("/api/v1/query", response_model=QueryResponse, responses=ERROR_RESPONSES)
def query(body: QueryRequest, request: Request, user: AuthenticatedUser = Depends(get_current_user)) -> QueryResponse:
    pipeline = get_pipeline(request)
    # Per-tenant limits: every query, and separately the ones that call Gemini
    # (the scarce resource). Sources-only queries do not use the answer budget.
    enforce("QUERIES", f"query:tenant:{user.tenant_id}",
            "Your organization is sending questions too quickly. Please wait a moment.")
    if not body.retrieve_only:
        enforce("ANSWERS", f"answer:tenant:{user.tenant_id}",
                "Your organization has used its AI answer allowance for now. You can still view matching sources.",
                code="answer_limit_reached")
    # Tenant, role and departments come only from the database, never from the request.
    result = pipeline.answer(
        body.question, top_k=body.top_k, retrieve_only=body.retrieve_only, scope=user.scope
    )
    return QueryResponse(
        question=result.question,
        answer=result.answer,
        citations=[Citation.from_result(number, source) for number, source in result.cited_sources()],
        sources=[Source.from_search_result(number, source) for number, source in enumerate(result.sources, 1)],
        removed_citations=result.removed_citations,
        llm_model=None if body.retrieve_only else pipeline.llm_model_name,
        timings=Timings(
            retrieval_ms=round(result.retrieval_ms, 1),
            generation_ms=None if result.generation_ms is None else round(result.generation_ms, 1),
        ),
    )


@app.get("/api/v1/health", response_model=HealthResponse, responses={503: {"model": HealthResponse}})
def health(request: Request):
    """ok: everything works | degraded: database fine, AI answers unavailable (sources-only
    still works) | unavailable (503): the database cannot be reached, nothing can be served."""
    pipeline = get_pipeline(request)
    llm_configured = pipeline.gemini_client is not None

    if is_production():
        # Public health output: only the state. No corpus sizes (they span tenants),
        # models, hardware or configuration; and only a trivial query.
        try:
            with connect_app() as conn:
                conn.execute("SELECT 1")
            status = "ok" if llm_configured else "degraded"
        except (psycopg.Error, PoolTimeout) as error:
            log.warning("health check: database unavailable (%s)", type(error).__name__)
            status = "unavailable"
        return JSONResponse(status_code=503 if status == "unavailable" else 200, content={"status": status})

    try:
        documents, chunks = pipeline.corpus_counts()
        database = DatabaseStatus(ok=True, documents=documents, chunks=chunks)
    except (psycopg.Error, PoolTimeout) as error:
        log.warning("Health check: database error: %s", error)
        database = DatabaseStatus(ok=False, error="Could not query PostgreSQL.")
    status = "unavailable" if not database.ok else "ok" if llm_configured else "degraded"
    body = HealthResponse(
        status=status,
        database=database,
        embedding_model=pipeline.embedding_model_name,
        embedding_device=pipeline.embedding_device,
        reranker_model=pipeline.reranker_model_name,
        reranker_device=pipeline.reranker_device,
        llm_model=pipeline.llm_model_name,
        llm_configured=llm_configured,
    )
    # Without the database no query can be served; without Gemini, retrieve_only still works.
    return JSONResponse(status_code=200 if database.ok else 503, content=body.model_dump())
