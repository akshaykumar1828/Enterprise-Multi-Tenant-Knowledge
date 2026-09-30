// The access token lives in localStorage so a page reload keeps the user
// logged in; logout removes it. Storage can be unavailable (private mode,
// blocked site data), so every access is guarded.
const KEY = "ek.accessToken";

export function readToken(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function saveToken(token: string): void {
  try {
    window.localStorage.setItem(KEY, token);
  } catch {
    // Without storage the session simply ends when the page is closed.
  }
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // nothing to clear
  }
}
