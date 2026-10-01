# Architecture

This page covers the system's components, how a request moves through them, and how the RAG pipeline works.
Access rules are in [AUTHORIZATION.md](AUTHORIZATION.md). Deployment, backups and day-to-day commands are in
[OPERATIONS.md](OPERATIONS.md).

## Components

```
Browser ──HTTPS──► Caddy (deploy/Caddyfile)
                    ├─ /        → frontend/dist  (React + Vite single-page app, static files)
                    └─ /api/*   → 127.0.0.1:8000 FastAPI (src/api), one uvicorn worker
                                    ├─ PostgreSQL 18 + pgvector 0.8 (127.0.0.1:5432), role rag_app
                                    ├─ sentence-transformers models (local, GPU if available)
                                    └─ Google Gemini API (answer generation only)
```

| Component | Code | Role |
|---|---|---|
| Frontend | `frontend/src` | Login and registration, chat with sources and citations, document list and upload, admin panel (departments, users, document access). Keeps the access token in `localStorage`, so it survives a page reload. |
| API | `src/api` | FastAPI app: `auth.py` (login, `/me`), `documents.py` (list, upload, delete), `admin.py` (Admin API), `main.py` (`/query`, `/health`, middleware), `settings.py` (production settings checks), `rate_limit.py` (rate limits stored in PostgreSQL). |
| RAG core | `src/rag` | Loading, chunking, embeddings, ingestion, retrieval, reranking, LLM prompt and citations, access filter, users and tenants, the admin service, backups. |
| Database | `src/rag/schema.sql` | Tables `tenants`, `users`, `departments`, `user_departments`, `documents`, `document_departments`, `document_chunks` (with the `vector(384)` embedding and a full-text `tsvector`), and `rate_limits`. |
| Reverse proxy | `deploy/Caddyfile` | TLS, security headers, static frontend, `/api` proxy, 11 MB request-body cap on `/api`. |
| Windows tasks | `deploy/*.ps1` | `\EnterpriseRAG\` scheduled tasks: RAG API, RAG Caddy, RAG Backup. |

Every row of every table carries a `tenant_id`. Composite foreign keys such as `(document_id, tenant_id)` mean the
schema itself refuses any link that crosses tenants.

## Request flow

1. **Caddy** terminates TLS and adds the security headers (HSTS, a strict CSP, `X-Frame-Options: DENY`, and others).
   It serves the static app, and forwards `/api/*` to `127.0.0.1:8000`. Request bodies over 11 MB are refused.
2. **FastAPI middleware**, outermost first:
   - `request_context` gives every response an `X-Request-ID` and writes one JSON access-log line (path only, never
     the query string). Unexpected errors return a generic 500 that carries the request id.
   - `reject_oversized_uploads` refuses an upload whose declared `Content-Length` is already too large.
   - `hide_api_docs_in_production` answers `/docs`, `/redoc` and `/openapi.json` with 404 when `APP_ENV=production`.
   - `UploadBodyLimit` (ASGI) stops reading an upload body as soon as it exceeds the limit.
3. **Authentication.** `get_current_user` validates the bearer JWT. It must be HS256 with `exp`, `iat` and `sub`,
   and `typ=access`. The user and their **AccessScope** (tenant, role, departments) are then loaded **from the
   database on every request**. The token carries no permissions.
4. **Rate limits**, in production only: per tenant on `/query`, plus a daily per-tenant cap on questions that are
   sent to Gemini.
5. **The route runs.**
   - `/query` runs the RAG pipeline under the caller's scope.
   - `/documents` lists, uploads and deletes documents, again under the caller's scope.
   - `/admin/*` additionally requires `scope.is_admin`.
6. **Response.** Sources and citations are built from the system's own retrieval metadata, never from LLM output.

## RAG pipeline

### Ingestion (`src/rag/loader.py`, `chunking.py`, `ingest.py`, `uploads.py`)

- **Formats:** `.md`, `.markdown`, `.txt` and `.pdf`. PDFs are read with pypdf, page by page.
- **Chunking:** chunks of about 500 characters with 100 characters of overlap. The splitter respects Markdown
  sections and records each chunk's section and page.
- **Contextual embeddings (`contextual-v1`).** Each chunk is embedded together with its document and section
  context. The embedding model is `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions), running locally.
- **Incremental sync.** Each document stores a SHA-256 content hash, so only new or changed files are re-embedded.
- **Two ways in:**
  - The folder corpus (`data/documents/`, 1,224 documents) belongs to the `default` tenant and is loaded with
    `python -m src.rag.ingest`.
  - Users can upload through `POST /api/v1/documents`. Uploads are checked against a byte limit, a PDF page limit, a
    text-character limit and a chunk limit, plus per-tenant document and storage quotas. Duplicates are detected
    within the caller's scope.

### Retrieval and answer (`src/rag/pipeline.py`, `retriever.py`, `llm.py`, `citations.py`)

1. **Embed the question** with the same model.
2. **Vector search in PostgreSQL**, top 50 candidates by cosine distance. The **access filter is part of the same SQL
   `WHERE` clause as the tenant filter**, and is applied before `ORDER BY`/`LIMIT`. Chunks the caller may not read are
   therefore never selected, never ranked, never reranked and never sent to the LLM.
3. **Rerank** the candidates with the `cross-encoder/ms-marco-MiniLM-L6-v2` cross-encoder.
4. **Diversify**, keeping at most 2 chunks per document, and take the top `top_k` (1–10, default 3).
5. **Generate** the answer with Gemini (`gemini-3.8-flash`, free tier) from the numbered sources only.
   `retrieve_only=true` skips this step and makes no Gemini call.
6. **Check citations.** Any `[n]` in the answer that doesn't match a retrieved source is removed and reported in
   `removed_citations`.

Both models load once at startup. One GPU lock serializes model work, which is why the API runs a single uvicorn
worker. With `HF_HUB_OFFLINE=1` (recommended in production) the models are loaded from the local cache and the
Hugging Face Hub isn't contacted.

`HybridRetriever` (vector plus full-text search, merged with Reciprocal Rank Fusion) exists for evaluation only. The
API uses `RerankingRetriever`.

## Data at a glance (production, 2026-10-01)

| Item | Count |
|---|---|
| Tenants | 1 (`default`) |
| Users | 1 (admin) |
| Departments | 8 |
| Documents | 1,224 (EnterpriseRAG-Bench slice: 1,222 files, plus the company handbook and the IT security policy) |
| Chunks | 22,018 |
| Document–department links | 1,373 |
