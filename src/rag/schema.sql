-- Persistence schema for the RAG system.
-- Safe to run repeatedly: every statement is idempotent. db.ensure_schema runs
-- the whole file in one transaction, so a failed migration changes nothing.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Tables (a fresh database gets these directly)
-- ---------------------------------------------------------------------------

-- One row per tenant (organization). Every document belongs to exactly one.
CREATE TABLE IF NOT EXISTS tenants (
    id         bigserial   PRIMARY KEY,
    slug       text        NOT NULL UNIQUE,       -- stable identifier, e.g. "default", "acme"
    name       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- The tenant that owns everything ingested before multi-tenancy existed.
INSERT INTO tenants (slug, name) VALUES ('default', 'Default tenant')
ON CONFLICT (slug) DO NOTHING;

-- Users belong to exactly one tenant and log in with a globally unique email.
-- Only an Argon2 hash of the password is stored, never the password itself.
CREATE TABLE IF NOT EXISTS users (
    id            bigserial   PRIMARY KEY,
    tenant_id     bigint      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email         text        NOT NULL,       -- stored lower-case
    display_name  text,
    password_hash text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- One row per source file.
CREATE TABLE IF NOT EXISTS documents (
    id              bigserial   PRIMARY KEY,
    tenant_id       bigint      NOT NULL REFERENCES tenants(id),
    relative_path   text        NOT NULL,         -- path under the tenant's documents folder, "/"-separated
    source          text        NOT NULL,         -- file name, for display
    source_type     text        NOT NULL,         -- "local", or the dataset folder: slack, gmail, jira, ...
    doc_id          text,                         -- EnterpriseRAG-Bench id (dsid_...), NULL for local files
    path            text        NOT NULL,
    content_hash    text        NOT NULL,         -- sha256 of the file bytes
    chunk_size      integer     NOT NULL,
    chunk_overlap   integer     NOT NULL,
    embedding_model text        NOT NULL,
    embedding_input_version text NOT NULL,        -- how chunk text was framed before embedding
    origin          text        NOT NULL DEFAULT 'folder',  -- 'folder' (server-side ingestion) or 'upload' (API)
    ingested_at     timestamptz NOT NULL DEFAULT now()
);

-- One row per chunk, with its metadata and its embedding.
-- all-MiniLM-L6-v2 produces 384-dimensional vectors.
-- No vector index yet: the dataset is small, so an exact scan is fine.
-- tenant_id is repeated here so retrieval can filter chunks directly; a
-- composite foreign key (below) guarantees it always equals the document's.
CREATE TABLE IF NOT EXISTS document_chunks (
    id          bigserial   PRIMARY KEY,
    tenant_id   bigint      NOT NULL,
    document_id bigint      NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id    text        NOT NULL,             -- e.g. sample_company_handbook.md#2 (unique per tenant)
    chunk_index integer     NOT NULL,
    page_number integer,                          -- NULL for Markdown/text files
    section     text        NOT NULL DEFAULT '',
    text        text        NOT NULL,
    start_char  integer     NOT NULL,
    end_char    integer     NOT NULL,
    embedding   vector(384) NOT NULL,
    lexical_vector tsvector,                      -- full-text index of title/source/section/text
    UNIQUE (document_id, chunk_index)
);

-- ---------------------------------------------------------------------------
-- Migrations for databases created by earlier versions (no-ops on a fresh one)
-- ---------------------------------------------------------------------------

-- v2: documents keyed by relative path instead of file name.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS relative_path text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_type text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS doc_id text;
UPDATE documents SET relative_path = source WHERE relative_path IS NULL;
UPDATE documents SET source_type = 'local' WHERE source_type IS NULL;
ALTER TABLE documents ALTER COLUMN relative_path SET NOT NULL;
ALTER TABLE documents ALTER COLUMN source_type SET NOT NULL;
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_source_key;

-- v3: contextual embeddings. Rows embedded from plain chunk text are marked
-- 'plain-v0' so the next ingestion re-embeds them.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS embedding_input_version text NOT NULL DEFAULT 'plain-v0';
ALTER TABLE documents ALTER COLUMN embedding_input_version DROP DEFAULT;

-- v4: full-text search. Existing chunks get NULL; ingestion fills them in.
ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS lexical_vector tsvector;
CREATE INDEX IF NOT EXISTS document_chunks_lexical_vector_idx ON document_chunks USING gin (lexical_vector);

-- v5: multi-tenancy. Everything that exists already belongs to the default tenant.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS tenant_id bigint REFERENCES tenants(id);
UPDATE documents SET tenant_id = (SELECT id FROM tenants WHERE slug = 'default') WHERE tenant_id IS NULL;
ALTER TABLE documents ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS tenant_id bigint;
UPDATE document_chunks c SET tenant_id = d.tenant_id
FROM documents d WHERE d.id = c.document_id AND c.tenant_id IS NULL;
ALTER TABLE document_chunks ALTER COLUMN tenant_id SET NOT NULL;

-- A chunk's tenant must be its document's tenant: enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS documents_id_tenant_key ON documents (id, tenant_id);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'document_chunks_document_tenant_fkey') THEN
        ALTER TABLE document_chunks
            ADD CONSTRAINT document_chunks_document_tenant_fkey
            FOREIGN KEY (document_id, tenant_id) REFERENCES documents (id, tenant_id) ON DELETE CASCADE;
    END IF;
END $$;

-- Uniqueness is per tenant: two tenants may hold files with the same path.
DROP INDEX IF EXISTS documents_relative_path_key;
DROP INDEX IF EXISTS documents_doc_id_key;
ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS document_chunks_chunk_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS documents_tenant_relative_path_key ON documents (tenant_id, relative_path);
CREATE UNIQUE INDEX IF NOT EXISTS documents_tenant_doc_id_key ON documents (tenant_id, doc_id) WHERE doc_id IS NOT NULL;
-- Also serves as the index for filtering chunks by tenant (tenant_id is its leading column).
CREATE UNIQUE INDEX IF NOT EXISTS document_chunks_tenant_chunk_id_key ON document_chunks (tenant_id, chunk_id);

-- v6: authentication. Users get a password hash, and an email identifies one
-- user (and so one tenant) across the whole system. Any user row created
-- before this migration gets an unusable hash ('!') and cannot log in until
-- its password is set.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
UPDATE users SET password_hash = '!' WHERE password_hash IS NULL;
ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_tenant_id_email_key;
CREATE UNIQUE INDEX IF NOT EXISTS users_email_key ON users (lower(email));
CREATE INDEX IF NOT EXISTS users_tenant_id_idx ON users (tenant_id);

-- v7: document uploads. Existing rows came from server-side folder ingestion.
-- Only 'upload' documents can be deleted through the API; 'folder' documents
-- are owned by the ingestion command (deleting them would be undone by the next sync).
ALTER TABLE documents ADD COLUMN IF NOT EXISTS origin text NOT NULL DEFAULT 'folder';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'documents_origin_check') THEN
        ALTER TABLE documents ADD CONSTRAINT documents_origin_check CHECK (origin IN ('folder', 'upload'));
    END IF;
END $$;
-- Listing a tenant's documents, newest first.
CREATE INDEX IF NOT EXISTS documents_tenant_ingested_idx ON documents (tenant_id, ingested_at DESC, id DESC);

-- v8: limits. Uploaded file size (for per-tenant storage limits; NULL for folder
-- documents, which do not count), and fixed-window rate-limit counters.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS size_bytes bigint;
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket       text        NOT NULL,   -- e.g. "register:<ip>", "upload:tenant:<id>"
    window_start timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    hits         integer     NOT NULL,
    PRIMARY KEY (bucket, window_start)
);
CREATE INDEX IF NOT EXISTS rate_limits_expires_idx ON rate_limits (expires_at);
