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

export type Role = "admin" | "employee";

export interface DepartmentInfo {
  id: number;
  slug: string;
  name: string;
}

/**
 * GET /auth/me. Role and departments are read from the database on every request.
 * The UI uses them only to decide what to show; the server enforces all access.
 */
export interface CurrentUser extends User {
  role: Role;
  departments: DepartmentInfo[];
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

// --- Admin API (/api/v1/admin) ---------------------------------------------------

export interface Department extends DepartmentInfo {
  member_count: number;
  document_count: number;
}

export interface CompanyUser {
  id: number;
  email: string;
  display_name: string | null;
  role: Role;
  department_ids: number[];
  created_at: string;
}

export interface CompanyUserList {
  total: number;
  limit: number;
  offset: number;
  items: CompanyUser[];
}

export type Visibility = "company" | "departments";

export interface DocumentAccess {
  id: number;
  filename: string;
  origin: string;
  visibility: Visibility;
  department_ids: number[];
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
