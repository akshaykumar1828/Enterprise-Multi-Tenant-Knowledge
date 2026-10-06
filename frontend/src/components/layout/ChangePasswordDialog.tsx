import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { ApiError } from "../../api/client";
import { changePassword } from "../../api/endpoints";
import { useAuth } from "../../auth/AuthContext";

const MIN_PASSWORD_LENGTH = 12; // matches the backend rule

/** Modal form: current password, new password twice. Escape or Cancel closes it; focus returns afterwards. */
export function ChangePasswordDialog({ onClose }: { onClose: () => void }) {
  const { token } = useAuth();
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const first = useRef<HTMLInputElement>(null);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    first.current?.focus();
    return () => previous?.focus?.();
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (next.length < MIN_PASSWORD_LENGTH) return setError(`The new password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
    if (next !== repeat) return setError("The two new passwords do not match.");
    if (next === current) return setError("The new password must be different from the current one.");
    if (!token) return;
    setSaving(true);
    try {
      await changePassword(token, current, next);
      setDone(true);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Something went wrong. Please try again.");
    } finally {
      setSaving(false);
    }
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    } else if (event.key === "Tab" && panel.current) {
      const items = Array.from(panel.current.querySelectorAll<HTMLElement>("input, button:not(:disabled)"));
      const [head, tail] = [items[0], items[items.length - 1]];
      if (event.shiftKey && document.activeElement === head) {
        event.preventDefault();
        tail.focus();
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault();
        head.focus();
      }
    }
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={panel} className="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={onKeyDown}>
        <h2 id={titleId} className="dialog__title">Change password</h2>
        {done ? (
          <>
            <p className="notice notice--success" role="status">Your password has been changed. Use it the next time you log in.</p>
            <div className="dialog__actions">
              <button type="button" className="button" onClick={onClose}>Done</button>
            </div>
          </>
        ) : (
          <form onSubmit={submit} className="password-form" aria-label="Change password">
            <div className="field">
              <label htmlFor="current-password">Current password</label>
              <input id="current-password" ref={first} type="password" autoComplete="current-password" required
                     value={current} onChange={(e) => setCurrent(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="new-password">New password</label>
              <input id="new-password" type="password" autoComplete="new-password" required minLength={MIN_PASSWORD_LENGTH}
                     maxLength={256} aria-describedby="new-password-hint" value={next} onChange={(e) => setNext(e.target.value)} />
              <p id="new-password-hint" className="field__hint">At least {MIN_PASSWORD_LENGTH} characters.</p>
            </div>
            <div className="field">
              <label htmlFor="repeat-password">Repeat new password</label>
              <input id="repeat-password" type="password" autoComplete="new-password" required
                     value={repeat} onChange={(e) => setRepeat(e.target.value)} />
            </div>
            {error && <p className="notice notice--error" role="alert">{error}</p>}
            <div className="dialog__actions">
              <button type="button" className="button button--ghost" onClick={onClose}>Cancel</button>
              <button type="submit" className="button" disabled={saving}>{saving ? "Saving…" : "Change password"}</button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
