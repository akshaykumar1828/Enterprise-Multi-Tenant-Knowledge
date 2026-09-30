import { useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

const MIN_PASSWORD_LENGTH = 12; // matches the backend rule

export function RegisterForm({ onShowLogin }: { onShowLogin: () => void }) {
  const { register } = useAuth();
  const [organizationName, setOrganizationName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    setSubmitting(true);
    try {
      await register({
        organization_name: organizationName.trim(),
        email: email.trim(),
        password,
        ...(displayName.trim() ? { display_name: displayName.trim() } : {}),
      });
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Registration failed. Please try again.");
      setSubmitting(false);
    }
  }

  return (
    <form className="card" onSubmit={onSubmit} aria-labelledby="register-title">
      <h1 id="register-title">Create an organization</h1>
      <p className="card__hint">This creates a new organization with you as its first user.</p>
      {error && <p className="alert alert--error" role="alert">{error}</p>}
      <label>
        Organization name
        <input required minLength={2} maxLength={100} value={organizationName}
               onChange={(e) => setOrganizationName(e.target.value)} />
      </label>
      <label>
        Your name <span className="optional">(optional)</span>
        <input maxLength={100} value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
      </label>
      <label>
        Email
        <input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      <label>
        Password <span className="optional">(at least {MIN_PASSWORD_LENGTH} characters)</span>
        <input type="password" autoComplete="new-password" required value={password}
               onChange={(e) => setPassword(e.target.value)} />
      </label>
      <button type="submit" className="button" disabled={submitting}>
        {submitting ? "Creating…" : "Create organization"}
      </button>
      <p className="card__switch">
        Already have an account?{" "}
        <button type="button" className="link" onClick={onShowLogin}>Log in</button>
      </p>
    </form>
  );
}
