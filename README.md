# Enterprise Multi-Tenant Knowledge

An enterprise knowledge platform. Organizations upload their documents and ask questions about them in plain language. Answers are grounded in those documents through retrieval-augmented generation (RAG).

## Current goal

Build a simple, working **local RAG core** first. That means extracting text from documents, generating embeddings and retrieving relevant passages. Everything else is built on top of it.

## Current technology (RAG prototype)

- Python 3.12 (virtual environment in `.venv`)
- PyTorch 2.14 with CUDA 13.2 (runs on the local NVIDIA GPU)
- sentence-transformers for local embeddings
- pypdf for PDF text extraction
- numpy for vector operations and similarity
- python-dotenv for configuration

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Project layout

| Path | Purpose |
|---|---|
| `src/rag/` | RAG core implementation |
| `data/documents/` | Local source documents for development |
| `data/processed/` | Generated artifacts (not committed) |
| `tests/` | Tests for the RAG components |
| `docs/` | Documentation and architecture notes |

## Status

**Phase 2: project structure.** Enterprise features will be added incrementally in later phases. These include the web frontend and API backend, PostgreSQL/pgvector storage, authentication, RBAC and multi-tenancy.
