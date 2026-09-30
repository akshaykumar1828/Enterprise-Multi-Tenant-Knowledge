import { useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export function LoginForm({ onShowRegister }: { onShowRegister: () => void }) {
  const { login, notice } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Login failed. Please try again.");
      setSubmitting(false);
    }
  }

  return (
    <form className="card" onSubmit={onSubmit} aria-labelledby="login-title">
      <h1 id="login-title">Log in</h1>
      {notice && <p className="alert alert--info" role="status">{notice}</p>}
      {error && <p className="alert alert--error" role="alert">{error}</p>}
      <label>
        Email
        <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      <label>
        Password
        <input type="password" autoComplete="current-password" required value={password}
               onChange={(e) => setPassword(e.target.value)} />
      </label>
      <button type="submit" className="button" disabled={submitting}>
        {submitting ? "Logging in…" : "Log in"}
      </button>
      <p className="card__switch">
        New organization?{" "}
        <button type="button" className="link" onClick={onShowRegister}>Create an account</button>
      </p>
    </form>
  );
}
