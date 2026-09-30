import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { setUnauthorizedHandler } from "../api/client";
import { fetchCurrentUser, login as loginRequest, registerOrganization } from "../api/endpoints";
import type { CurrentUser, RegisterRequest } from "../api/types";
import { clearToken, readToken, saveToken } from "./tokenStorage";

type Status = "checking" | "anonymous" | "authenticated";

interface AuthState {
  status: Status;
  /**
   * From GET /auth/me, including role and departments. Only used to decide what to
   * show: the server re-checks every request, so this is never a security boundary.
   */
  user: CurrentUser | null;
  token: string | null;
  /** Shown on the login screen, e.g. after a session expired. */
  notice: string | null;
  login: (email: string, password: string) => Promise<void>;
  register: (request: RegisterRequest) => Promise<void>;
  logout: () => void;
  /** Re-read /auth/me, e.g. after the server said this user is no longer an admin. */
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => readToken());
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [status, setStatus] = useState<Status>(() => (readToken() ? "checking" : "anonymous"));
  const [notice, setNotice] = useState<string | null>(null);

  const endSession = useCallback((message: string | null) => {
    clearToken();
    setToken(null);
    setUser(null);
    setStatus("anonymous");
    setNotice(message);
  }, []);

  // Any authenticated request answered with 401 sends the user back to login.
  useEffect(() => {
    setUnauthorizedHandler(() => endSession("Your session has expired. Please log in again."));
    return () => setUnauthorizedHandler(null);
  }, [endSession]);

  // On page load, validate a stored token by asking who it belongs to.
  useEffect(() => {
    if (status !== "checking" || !token) return;
    let cancelled = false;
    fetchCurrentUser(token)
      .then((current) => {
        if (cancelled) return;
        setUser(current);
        setStatus("authenticated");
      })
      .catch(() => {
        if (!cancelled) endSession(null);
      });
    return () => {
      cancelled = true;
    };
  }, [status, token, endSession]);

  const login = useCallback(async (email: string, password: string) => {
    const { access_token } = await loginRequest(email, password);
    const current = await fetchCurrentUser(access_token);
    saveToken(access_token);
    setToken(access_token);
    setUser(current);
    setNotice(null);
    setStatus("authenticated");
  }, []);

  const register = useCallback(
    async (request: RegisterRequest) => {
      await registerOrganization(request);
      await login(request.email, request.password);
    },
    [login],
  );

  const logout = useCallback(() => endSession(null), [endSession]);

  const refreshUser = useCallback(async () => {
    if (!token) return;
    try {
      setUser(await fetchCurrentUser(token));
    } catch {
      // A 401 already ends the session (unauthorized handler); other errors keep the
      // current state, and the server keeps enforcing access either way.
    }
  }, [token]);

  const value = useMemo(
    () => ({ status, user, token, notice, login, register, logout, refreshUser }),
    [status, user, token, notice, login, register, logout, refreshUser],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>");
  return context;
}
