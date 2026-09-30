import { apiRequest } from "./client";
import type {
  CompanyUser,
  CompanyUserList,
  CurrentUser,
  Department,
  DocumentAccess,
  DocumentList,
  KnowledgeDocument,
  QueryRequest,
  QueryResponse,
  RegisterRequest,
  Role,
  TokenResponse,
  User,
  Visibility,
} from "./types";

export const registerOrganization = (request: RegisterRequest) =>
  apiRequest<User>("/api/v1/auth/register", { method: "POST", body: request });

export const login = (email: string, password: string) =>
  apiRequest<TokenResponse>("/api/v1/auth/login", { method: "POST", body: { email, password } });

export const fetchCurrentUser = (token: string) => apiRequest<CurrentUser>("/api/v1/auth/me", { token });

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

// Admin API. The server checks on every call that the token's user is currently an
// admin of their own company; nothing here (tenant, role, flags) decides access.
const ADMIN = "/api/v1/admin";

export const listDepartments = (token: string) => apiRequest<Department[]>(`${ADMIN}/departments`, { token });

export const createDepartment = (token: string, slug: string, name: string) =>
  apiRequest<Department>(`${ADMIN}/departments`, { method: "POST", body: { slug, name }, token });

export const updateDepartment = (token: string, id: number, changes: { slug?: string; name?: string }) =>
  apiRequest<Department>(`${ADMIN}/departments/${id}`, { method: "PATCH", body: changes, token });

export const deleteDepartment = (token: string, id: number) =>
  apiRequest<void>(`${ADMIN}/departments/${id}`, { method: "DELETE", token });

export const listCompanyUsers = (token: string, limit: number, offset: number) =>
  apiRequest<CompanyUserList>(`${ADMIN}/users?limit=${limit}&offset=${offset}`, { token });

export const setUserRole = (token: string, userId: number, role: Role) =>
  apiRequest<CompanyUser>(`${ADMIN}/users/${userId}/role`, { method: "PUT", body: { role }, token });

export const addUserToDepartment = (token: string, userId: number, departmentId: number) =>
  apiRequest<CompanyUser>(`${ADMIN}/users/${userId}/departments/${departmentId}`, { method: "PUT", token });

export const removeUserFromDepartment = (token: string, userId: number, departmentId: number) =>
  apiRequest<CompanyUser>(`${ADMIN}/users/${userId}/departments/${departmentId}`, { method: "DELETE", token });

export const getDocumentAccess = (token: string, documentId: number) =>
  apiRequest<DocumentAccess>(`${ADMIN}/documents/${documentId}/access`, { token });

export const setDocumentAccess = (token: string, documentId: number, visibility: Visibility, departmentIds: number[]) =>
  apiRequest<DocumentAccess>(`${ADMIN}/documents/${documentId}/access`, {
    method: "PUT",
    body: { visibility, department_ids: visibility === "departments" ? departmentIds : [] },
    token,
  });
