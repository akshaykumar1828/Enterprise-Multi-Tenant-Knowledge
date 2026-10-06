"""Request and response models for the API."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.rag.retriever import SearchResult
from src.rag.uploads import DocumentInfo
from src.rag.users import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, User, normalize_email


# --- authentication ------------------------------------------------------------

class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_name: str = Field(min_length=2, max_length=100, description="Name of the new organization (tenant).")
    email: str = Field(max_length=254)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    display_name: str | None = Field(None, max_length=100)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("organization_name")
    @classmethod
    def organization_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("organization_name must not be blank")
        return value.strip()


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until the token expires.")


class TenantInfo(BaseModel):
    slug: str
    name: str


class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str | None
    tenant: TenantInfo

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(id=user.id, email=user.email, display_name=user.display_name,
                   tenant=TenantInfo(slug=user.tenant_slug, name=user.tenant_name))


class DepartmentInfo(BaseModel):
    id: int
    slug: str
    name: str


class CurrentUserResponse(UserResponse):
    """GET /auth/me: the user plus their current role and departments, read from the
    database for this request. For display only; the server enforces access itself."""

    role: Literal["admin", "employee"]
    departments: list[DepartmentInfo]


# --- documents (knowledge base) ----------------------------------------------------

class DocumentResponse(BaseModel):
    id: int
    filename: str
    source_type: str
    origin: str = Field(description="'upload' (added through the API) or 'folder' (server-side ingestion).")
    chunk_count: int
    ingested_at: datetime
    deletable: bool = Field(description="Only uploaded documents can be deleted through the API.")
    description: str | None = Field(
        None, description="One or two sentences taken from the document's opening text (no AI); null if none.")

    @classmethod
    def from_info(cls, info: DocumentInfo) -> "DocumentResponse":
        return cls(id=info.id, filename=info.filename, source_type=info.source_type, origin=info.origin,
                   chunk_count=info.chunk_count, ingested_at=info.ingested_at, deletable=info.deletable,
                   description=info.description)


class DocumentListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[DocumentResponse]


# --- administration ------------------------------------------------------------------

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("name must not be blank")
    return value


class DepartmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN, description="Stable identifier: lowercase letters, digits and '-'.")
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        return _clean_name(value)


class DepartmentUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str | None = Field(None, pattern=SLUG_PATTERN)
    name: str | None = Field(None, min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str | None) -> str | None:
        return _clean_name(value)


class DepartmentResponse(BaseModel):
    id: int
    slug: str
    name: str
    member_count: int
    document_count: int


class CompanyUserResponse(BaseModel):
    """A user as an admin sees it; never includes a password or its hash."""

    id: int
    email: str
    display_name: str | None
    role: Literal["admin", "employee"]
    department_ids: list[int]
    created_at: datetime


class CompanyUserListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CompanyUserResponse]


class EmployeeCreateRequest(BaseModel):
    """A new employee of the admin's own company. No tenant or role field: the account is always
    an employee of the admin's tenant (promote with PUT /users/{id}/role)."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=254)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH,
                          description="Initial password; share it with the employee securely.")
    display_name: str | None = Field(None, max_length=100)
    department_ids: list[int] = Field(default_factory=list, max_length=50)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "employee"]


class DocumentAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visibility: Literal["company", "departments"]
    department_ids: list[int] = Field(default_factory=list, max_length=500,
                                      description="For 'departments' only; an empty list means admins only.")

    @model_validator(mode="after")
    def company_has_no_departments(self) -> "DocumentAccessRequest":
        if self.visibility == "company" and self.department_ids:
            raise ValueError("department_ids must be empty when visibility is 'company'")
        return self


class DocumentAccessResponse(BaseModel):
    id: int
    filename: str
    origin: str
    visibility: Literal["company", "departments"]
    department_ids: list[int]


# --- RAG query -----------------------------------------------------------------


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000, description="The question to answer.")
    top_k: int = Field(3, ge=1, le=10, description="How many sources to retrieve and send to the LLM.")
    retrieve_only: bool = Field(False, description="Return sources without generating an answer (no Gemini call).")
    # There is deliberately no tenant field: the tenant comes from the authenticated
    # user, and extra="forbid" rejects any attempt to send one (422).

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value


class SourceMetadata(BaseModel):
    number: int = Field(description="The [n] used for this source in the answer.")
    source: str
    relative_path: str
    source_type: str
    doc_id: str | None
    page_number: int | None
    section: str | None
    chunk_id: str

    @classmethod
    def from_result(cls, number: int, result: SearchResult, **extra):
        return cls(
            number=number,
            source=result.source,
            relative_path=result.relative_path,
            source_type=result.source_type,
            doc_id=result.doc_id,
            page_number=result.page_number,
            section=result.section or None,
            chunk_id=result.chunk_id,
            **extra,
        )


class Citation(SourceMetadata):
    """A source the answer actually cites."""


class Source(SourceMetadata):
    """A retrieved chunk that was sent to the LLM as context."""

    similarity: float
    rerank_score: float | None
    text: str

    @classmethod
    def from_search_result(cls, number: int, result: SearchResult) -> "Source":
        return cls.from_result(
            number, result, similarity=round(result.score, 4),
            rerank_score=None if result.rerank_score is None else round(result.rerank_score, 4),
            text=result.text,
        )


class Timings(BaseModel):
    retrieval_ms: float
    generation_ms: float | None


class QueryResponse(BaseModel):
    question: str
    answer: str | None = Field(description="Grounded answer with [n] citations; null when retrieve_only.")
    citations: list[Citation]
    sources: list[Source]
    removed_citations: list[int] = Field(description="Citation numbers the LLM used that matched no source.")
    llm_model: str | None
    timings: Timings


class DatabaseStatus(BaseModel):
    ok: bool
    documents: int | None = None
    chunks: int | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str = Field(description="'ok'; 'degraded' (AI answers unavailable, sources still work); "
                                    "'unavailable' (database unreachable, HTTP 503).")
    database: DatabaseStatus
    embedding_model: str
    embedding_device: str
    reranker_model: str
    reranker_device: str
    llm_model: str
    llm_configured: bool


class ErrorDetail(BaseModel):
    code: str
    message: str
    retry_after_seconds: float | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
