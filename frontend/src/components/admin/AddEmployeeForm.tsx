import { useState, type FormEvent } from "react";

import type { Department } from "../../api/types";

export const MIN_PASSWORD_LENGTH = 12; // matches the backend rule

/** A random initial password (browser crypto), easy to read out: no look-alike characters. */
export function generatePassword(length = 16): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
  const bytes = new Uint32Array(length);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

export interface NewEmployee {
  email: string;
  password: string;
  display_name?: string;
  department_ids: number[];
}

/** Form to add an employee: name, email, initial password, departments. Validates before calling the API. */
export function AddEmployeeForm({ departments, busy, onSubmit, onCancel }: {
  departments: Department[];
  busy: boolean;
  onSubmit: (employee: NewEmployee) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState(() => generatePassword());
  const [chosen, setChosen] = useState<number[]>([]);
  const [problem, setProblem] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (password.length < MIN_PASSWORD_LENGTH) {
      setProblem(`The password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    setProblem(null);
    const ok = await onSubmit({
      email: email.trim(), password, department_ids: chosen,
      ...(name.trim() ? { display_name: name.trim() } : {}),
    });
    if (ok) {
      setName("");
      setEmail("");
      setPassword(generatePassword());
      setChosen([]);
    }
  }

  const toggle = (id: number) => setChosen((current) => (current.includes(id) ? current.filter((x) => x !== id) : [...current, id]));

  return (
    <form onSubmit={submit} className="panel employee-form" aria-label="Add employee">
      <div className="employee-form__fields">
        <div className="field">
          <label htmlFor="employee-name">Full name <span className="optional">(optional)</span></label>
          <input id="employee-name" value={name} maxLength={100} autoFocus onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="employee-email">Work email</label>
          <input id="employee-email" type="email" required maxLength={254} autoComplete="off" value={email}
                 onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="employee-password">Initial password</label>
          <div className="row-form">
            <input id="employee-password" type="text" required autoComplete="new-password" spellCheck={false}
                   minLength={MIN_PASSWORD_LENGTH} maxLength={256} value={password}
                   aria-describedby="employee-password-hint" onChange={(e) => setPassword(e.target.value)} />
            <button type="button" className="button button--ghost button--small" onClick={() => setPassword(generatePassword())}>
              Generate
            </button>
          </div>
          <p id="employee-password-hint" className="field__hint">
            At least {MIN_PASSWORD_LENGTH} characters. Share it with the employee privately.
          </p>
        </div>
      </div>
      <fieldset className="employee-form__departments">
        <legend>Departments</legend>
        {departments.length === 0 && <p className="muted small">No departments exist yet. Create them in the Departments tab.</p>}
        <div className="choice-grid">
          {departments.map((department) => (
            <label key={department.id} className="choice">
              <input type="checkbox" checked={chosen.includes(department.id)} onChange={() => toggle(department.id)} />
              {department.name}
            </label>
          ))}
        </div>
        {departments.length > 0 && chosen.length === 0 && (
          <p className="field__hint">Without a department, the employee only sees company-wide documents.</p>
        )}
      </fieldset>
      {problem && <p className="notice notice--error" role="alert">{problem}</p>}
      <div className="inline-form__actions">
        <button type="submit" className="button" disabled={busy || !email.trim()}>
          {busy ? "Adding…" : "Add employee"}
        </button>
        <button type="button" className="button button--ghost" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
