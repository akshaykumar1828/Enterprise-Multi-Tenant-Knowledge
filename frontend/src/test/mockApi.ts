import { vi } from "vitest";

import type { QueryResponse, User } from "../api/types";

export interface RecordedCall {
  method: string;
  path: string;
  headers: Record<string, string>;
  body: unknown;
}

type Handler = (call: RecordedCall) => { status: number; body: unknown };

export const TOKEN = "test.jwt.token";

export const USER: User = {
  id: 1,
  email: "dev@example.com",
  display_name: "Dev User",
  tenant: { slug: "acme-1a2b3c", name: "Acme Corp" },
};

export const ANSWER: QueryResponse = {
  question: "How many annual leave days do employees receive?",
  answer: "Employees receive 20 days of annual paid leave per calendar year [1].",
  citations: [{
    number: 1, source: "sample_company_handbook.md", relative_path: "sample_company_handbook.md",
    source_type: "local", doc_id: null, page_number: null, section: "Annual Leave",
    chunk_id: "sample_company_handbook.md#2",
  }],
  sources: [
    {
      number: 1, source: "sample_company_handbook.md", relative_path: "sample_company_handbook.md",
      source_type: "local", doc_id: null, page_number: null, section: "Annual Leave",
      chunk_id: "sample_company_handbook.md#2", similarity: 0.564, rerank_score: 9.75,
      text: "Employees receive 20 days of annual paid leave per calendar year.",
    },
    {
      number: 2, source: "sample_it_security_policy.pdf", relative_path: "sample_it_security_policy.pdf",
      source_type: "local", doc_id: null, page_number: 3, section: null,
      chunk_id: "sample_it_security_policy.pdf#2", similarity: 0.41, rerank_score: -3.2,
      text: "Customer records are retained for 7 years after the end of the customer contract.",
    },
  ],
  removed_citations: [],
  llm_model: "gemini-2.5-flash",
  timings: { retrieval_ms: 320, generation_ms: 900 },
};

const json = (status: number, body: unknown) => ({ status, body });

/** Default happy-path backend; individual tests override single routes. */
export function defaultRoutes(): Record<string, Handler> {
  return {
    "POST /api/v1/auth/login": () => json(200, { access_token: TOKEN, token_type: "bearer", expires_in: 3600 }),
    "POST /api/v1/auth/register": (call) =>
      json(201, { ...USER, email: (call.body as { email: string }).email }),
    "GET /api/v1/auth/me": (call) =>
      call.headers.Authorization === `Bearer ${TOKEN}`
        ? json(200, USER)
        : json(401, { error: { code: "invalid_token", message: "The access token is invalid." } }),
    "POST /api/v1/query": () => json(200, ANSWER),
  };
}

/** Replace fetch with an in-memory API. Returns the list of calls the app made. */
export function mockApi(overrides: Record<string, Handler> = {}): RecordedCall[] {
  const routes = { ...defaultRoutes(), ...overrides };
  const calls: RecordedCall[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const raw = init?.body;
    const call: RecordedCall = {
      method: init?.method ?? "GET",
      path: url.pathname + url.search,
      headers: { ...(init?.headers as Record<string, string>) },
      body: raw instanceof FormData ? raw : raw ? JSON.parse(String(raw)) : undefined,
    };
    calls.push(call);
    // Exact match first, then the same path with a trailing numeric id as ":id", then without the query.
    const key = `${call.method} ${url.pathname}`;
    const handler = routes[`${call.method} ${call.path}`] ?? routes[key] ?? routes[key.replace(/\/\d+$/, "/:id")];
    const { status, body } = handler ? handler(call) : json(404, { detail: "Not Found" });
    if (status === 204) return new Response(null, { status });
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  });
  return calls;
}
