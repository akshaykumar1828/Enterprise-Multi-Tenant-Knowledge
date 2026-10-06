# Running with Docker

Four containers, started from this folder:

| Container | What it does | Reachable from |
|---|---|---|
| `web` | Caddy: serves the React app and forwards `/api` to `api` | `http://localhost:8180` (this computer only) |
| `api` | FastAPI, embedding model and reranker (CPU) | `web` only |
| `migrate` | Runs once per start as the database owner: schema migrations, the API's role and grants | — (exits) |
| `db` | PostgreSQL 18 + pgvector | `migrate` and `api` only |

Written answers come from **Ollama on the host** (it keeps using the GPU). Install it, then
`ollama pull gemma3:4b-it-q4_K_M`. The containers reach it at `host.docker.internal:11434`.

## Where the data lives

Nothing is synced automatically. Docker keeps the data in named volumes, so it survives
`docker compose down`, restarts and image rebuilds:

| Volume | Contents |
|---|---|
| `pgdata` | Users, departments, access rules, documents, chunks and embeddings |
| `uploads` | Files uploaded through the website |
| `caddy` | Caddy's state and access logs |

`docker compose down -v` **deletes** these volumes, and with them all data.

## First start

```powershell
cd docker
copy .env.example .env      # then fill in NEW random values, see the comments in the file
docker compose up -d --build
docker compose ps           # api: healthy, web: running
```

A fresh database is empty. Either move existing data in (next section), or create the first
admin and load documents:

```powershell
docker compose run --rm migrate python -m src.rag.users create --tenant default --email admin@example.com
docker compose run --rm migrate python -m src.rag.users set-role --email admin@example.com --role admin
docker compose run --rm migrate python -m src.rag.tenants rename --name "Redwood Inference"
```

Documents can then be uploaded from the website (Knowledge base → Upload).

## Moving an existing installation into Docker

Move the database and the uploaded files together, taken at the same moment: each upload is a
file plus its database row.

```powershell
# 1. On the existing installation (owner connection), dump the database.
.\.venv\Scripts\python.exe -m src.rag.backup create --out C:\rag-backups\to-docker.dump

# 2. Start only the database container and restore into it.
cd docker
docker compose up -d db
docker cp C:\rag-backups\to-docker.dump redwood-knowledge-db-1:/tmp/restore.dump
docker compose exec db sh -c 'pg_restore --exit-on-error --single-transaction --no-owner --no-privileges -U "$POSTGRES_USER" -d enterprise_rag /tmp/restore.dump && rm /tmp/restore.dump'

# 3. Start everything (migrate re-creates the API role and grants), then copy the uploads.
docker compose up -d
docker compose cp C:\rag-data\uploads\. api:/data/uploads
```

Users keep their passwords: the password hashes move with the database.

## Everyday commands

| Purpose | Command (from `docker/`) |
|---|---|
| Start / stop | `docker compose up -d` / `docker compose stop` |
| Rebuild after code changes | `docker compose up -d --build` |
| Logs | `docker compose logs -f api` |
| Backup | `docker compose exec db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" enterprise_rag' > backup.dump` |
| Database shell | `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" enterprise_rag'` |
| Operator commands | `docker compose run --rm migrate python -m src.rag.users set-password --email <email>` |

The site listens on 127.0.0.1 only. Publishing it on a domain with HTTPS works like the
Windows deployment (see `deploy/PRODUCTION_RUNBOOK.md`): set `SITE_ADDRESS` on the `web`
service, and map ports 80 and 443.
