// Base URL of the FastAPI server as seen by the browser. Empty means "same
// origin", which in development is the Vite server forwarding /api to FastAPI.
export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");
