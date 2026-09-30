// Mirrors of the FastAPI request/response models (src/api/schemas.py).

export interface Tenant {
  slug: string;
  name: string;
}

export interface User {
  id: number;
  email: string;
  display_name: string | null;
  tenant: Tenant;
}

export interface RegisterRequest {
  organization_name: string;
  email: string;
  password: string;
  display_name?: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

// Deliberately no tenant field: the backend derives the tenant from the token.
export interface QueryRequest {
  question: string;
  top_k?: number;
  retrieve_only?: boolean;
}

export interface KnowledgeDocument {
  id: number;
  filename: string;
  source_type: string;
  /** "upload" (added here) or "folder" (server-side ingestion, read-only here). */
  origin: string;
  chunk_count: number;
  ingested_at: string;
  deletable: boolean;
}

export interface DocumentList {
  total: number;
  limit: number;
  offset: number;
  items: KnowledgeDocument[];
}

export interface SourceMetadata {
  number: number;
  source: string;
  relative_path: string;
  source_type: string;
  doc_id: string | null;
  page_number: number | null;
  section: string | null;
  chunk_id: string;
}

export type Citation = SourceMetadata;

export interface Source extends SourceMetadata {
  similarity: number;
  rerank_score: number | null;
  text: string;
}

export interface QueryResponse {
  question: string;
  answer: string | null;
  citations: Citation[];
  sources: Source[];
  removed_citations: number[];
  llm_model: string | null;
  timings: { retrieval_ms: number; generation_ms: number | null };
}
