import { API_BASE_URL } from "../config";

/** An error response from the API, normalized from both of its error formats. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// Called when an authenticated request is rejected with 401 (expired or invalid
// token). The auth layer registers a handler that logs the user out.
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

interface RequestOptions {
  method?: "GET" | "POST" | "DELETE";
  /** A JSON-serializable value, or FormData for file uploads. */
  body?: unknown;
  token?: string | null;
}

export async function apiRequest<T>(path: string, { method = "GET", body, token }: RequestOptions = {}): Promise<T> {
  const isForm = body instanceof FormData;
  const headers: Record<string, string> = { Accept: "application/json" };
  // For FormData the browser sets the multipart Content-Type (with its boundary) itself.
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network_error", "Could not reach the server. Is the backend running?");
  }

  if (response.status === 204) return undefined as T;
  const payload = await response.json().catch(() => null);
  if (response.ok) return payload as T;

  const error = toApiError(response.status, payload);
  if (response.status === 401 && token) unauthorizedHandler?.();
  throw error;
}

function toApiError(status: number, payload: unknown): ApiError {
  // Our handlers: {"error": {"code", "message", "retry_after_seconds"}}
  const detail = (payload as { error?: { code?: string; message?: string; retry_after_seconds?: number | null } })?.error;
  if (detail?.code) {
    return new ApiError(status, detail.code, detail.message ?? "Request failed.", detail.retry_after_seconds ?? null);
  }
  // FastAPI validation errors: {"detail": [{"loc": [...], "msg": "..."}]}
  const validation = (payload as { detail?: Array<{ msg?: string; loc?: unknown[] }> | string })?.detail;
  if (Array.isArray(validation)) {
    const messages = validation.map((item) => {
      const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : undefined;
      const text = (item.msg ?? "is invalid").replace(/^Value error, /, "");
      return field ? `${String(field)}: ${text}` : text;
    });
    return new ApiError(status, "validation_error", messages.join("; "));
  }
  if (typeof validation === "string") return new ApiError(status, "error", validation);
  return new ApiError(status, "error", `Request failed (HTTP ${status}).`);
}
