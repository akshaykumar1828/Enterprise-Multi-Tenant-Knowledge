# Enterprise Multi-Tenant Knowledge

An enterprise knowledge platform. Organizations store their documents and ask questions about them in plain language.
Answers are grounded in those documents through retrieval-augmented generation (RAG), with numbered citations. Every
user sees only the documents their role and departments allow.

## Features

- **RAG:**
  - local embeddings (`all-MiniLM-L6-v2`) stored in PostgreSQL + pgvector;
  - cross-encoder reranking (`ms-marco-MiniLM-L6-v2`), keeping at most 2 chunks per document;
  - answers from a local model through Ollama (`gemma3:4b`, free, no usage limits, documents never leave the
    server), or optionally Gemini (`gemini-3.8-flash`, free tier), with checked citations;
  - a retrieve-only mode that makes no LLM call.
- **Multi-tenant.** Every row is tenant-scoped; composite foreign keys block cross-tenant links.
- **Authorization:**
  - roles `admin` and `employee`, plus departments;
  - document visibility is `company` or `departments`;
  - the access filter runs inside SQL before ranking, so unauthorized text never reaches the reranker or the LLM.
- **Admin API and admin panel** for departments, user roles and memberships, and per-document access.
- **Uploads** of PDF, Markdown and text files, with per-file and per-tenant limits.
- **Production hardening:**
  - a least-privilege database role, loopback-only services and Caddy with security headers;
  - rate limits and startup safety checks;
  - verified backups, run daily on a schedule.

## Technology

Python 3.12 · FastAPI · PostgreSQL 18 + pgvector 0.8 (psycopg 3) · sentence-transformers (CUDA when available) ·
Google GenAI SDK · React + Vite + TypeScript (Vitest) · Caddy · Windows Task Scheduler.

## Quick start (development)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Settings: create .env.development with the database owner's connection (PGHOST, PGPORT, PGDATABASE, PGUSER,
# PGPASSWORD), JWT_SECRET_KEY and optionally GEMINI_API_KEY. deploy\env.production.example lists every setting.
# The tests read .env.test or .env.development and never a file containing APP_ENV=production.
.\.venv\Scripts\python.exe -m src.rag.ingest                                   # load data/documents into the default tenant
.\.venv\Scripts\python.exe -m src.rag "What home-office stipend do new employees get?" --retrieve-only
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"        # backend tests (isolated database copy)
cd frontend; npm install; npm test; npm run dev
```

Production deployment is a separate procedure; see the documentation below.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, request flow, RAG pipeline, data model |
| [docs/AUTHORIZATION.md](docs/AUTHORIZATION.md) | Authentication, access rules, the 8 departments, document visibility, admin-only and pending-review documents, the 2026-10-01 rollout |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Deployment overview, backup and restore, restore points, security hardening, tests and results, commands, git checkpoint, open items |
| [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md) | Production deployment, step by step |
| [deploy/PRODUCTION_RUNBOOK.md](deploy/PRODUCTION_RUNBOOK.md) | Administrator steps: firewall, PostgreSQL, Caddy, startup tasks, HTTPS |
| [deploy/BACKUP.md](deploy/BACKUP.md) | Backup, verification, recovery, scheduled backups |
| [data/documents/enterprise_rag_bench/README.md](data/documents/enterprise_rag_bench/README.md) | The benchmark corpus (synthetic, MIT) |

## Project layout

| Path | Purpose |
|---|---|
| `src/api/` | FastAPI app: auth, documents, admin, query, health, settings, rate limits |
| `src/rag/` | RAG core, access filter, admin service, users and tenants, ingestion, uploads, backups, schema |
| `frontend/` | React single-page app |
| `deploy/` | Caddyfile, production env template, Windows task scripts, deployment and backup guides |
| `tests/` | Backend tests (run against a throwaway copy of the database) |
| `data/documents/` | Folder corpus for the default tenant (1,222 documents) |
| `docs/` | Architecture, authorization and operations documentation |

## Status (2026-10-01)

Production is live on a single machine:
- 1 tenant, with 1,222 documents and 21,999 chunks (the two sample files were removed on 2026-10-03).
- 8 departments.
- Access applied and verified: 31 company-wide, 1,166 department, 9 admin-only and 16 pending review.
- Tests pass: 218 backend and 39 frontend.
- Current git checkpoint: `1992fbf` ("Checkpoint: complete production authorization rollout").
- Open items are listed in [docs/OPERATIONS.md](docs/OPERATIONS.md#open-items).
