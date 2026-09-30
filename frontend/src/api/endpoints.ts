import { apiRequest } from "./client";
import type {
  DocumentList,
  KnowledgeDocument,
  QueryRequest,
  QueryResponse,
  RegisterRequest,
  TokenResponse,
  User,
} from "./types";

export const registerOrganization = (request: RegisterRequest) =>
  apiRequest<User>("/api/v1/auth/register", { method: "POST", body: request });

export const login = (email: string, password: string) =>
  apiRequest<TokenResponse>("/api/v1/auth/login", { method: "POST", body: { email, password } });

export const fetchCurrentUser = (token: string) => apiRequest<User>("/api/v1/auth/me", { token });

export const askQuestion = (token: string, request: QueryRequest) =>
  apiRequest<QueryResponse>("/api/v1/query", { method: "POST", body: request, token });

// Knowledge base. The tenant is always the logged-in user's; it is never sent.
export const listDocuments = (token: string, limit: number, offset: number) =>
  apiRequest<DocumentList>(`/api/v1/documents?limit=${limit}&offset=${offset}`, { token });

export function uploadDocument(token: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return apiRequest<KnowledgeDocument>("/api/v1/documents", { method: "POST", body: form, token });
}

export const deleteDocument = (token: string, id: number) =>
  apiRequest<void>(`/api/v1/documents/${id}`, { method: "DELETE", token });
