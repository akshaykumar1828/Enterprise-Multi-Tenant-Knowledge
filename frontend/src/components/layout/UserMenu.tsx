import { useEffect, useId, useRef, useState } from "react";

import { useAuth } from "../../auth/AuthContext";
import { initials } from "../../utils/documents";
import { Icon } from "../ui/Icon";
import { ChangePasswordDialog } from "./ChangePasswordDialog";

/** Profile button in the top bar; opens a small panel with who you are, Change password and Log out. */
export function UserMenu() {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const panelId = useId();
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    return () => document.removeEventListener("mousedown", onPointer);
  }, [open]);

  if (!user) return null;
  const name = user.display_name ?? user.email;
  const departments = user.departments.map((d) => d.name).join(", ");

  return (
    <div className="user-menu" ref={root}
         onKeyDown={(event) => event.key === "Escape" && open && (setOpen(false), event.stopPropagation())}>
      <button type="button" className="user-menu__button" aria-expanded={open} aria-controls={panelId}
              aria-label={`Account: ${name}`} onClick={() => setOpen((value) => !value)}>
        <span className="avatar" aria-hidden="true">{initials(name)}</span>
        <span className="user-menu__name">{name}</span>
        <span className="user-menu__chevron" aria-hidden="true"><Icon name="chevron" size={14} /></span>
      </button>
      {open && (
        <div id={panelId} className="user-menu__panel" role="region" aria-label="Your account">
          <div className="user-menu__identity">
            <span className="avatar avatar--large" aria-hidden="true">{initials(name)}</span>
            <div>
              <p className="user-menu__full-name">{name}</p>
              {user.display_name && <p className="muted small">{user.email}</p>}
            </div>
          </div>
          <dl className="user-menu__facts">
            <div><dt>Organization</dt><dd>{user.tenant.name}</dd></div>
            <div><dt>Role</dt><dd>{user.role === "admin" ? "Administrator" : "Employee"}</dd></div>
            <div><dt>Departments</dt><dd>{departments || "None assigned"}</dd></div>
          </dl>
          <div className="user-menu__actions">
            <button type="button" className="button button--ghost button--block"
                    onClick={() => { setOpen(false); setChangingPassword(true); }}>
              Change password
            </button>
            <button type="button" className="button button--ghost button--block" onClick={logout}>
              <Icon name="logout" size={16} /> Log out
            </button>
          </div>
        </div>
      )}
      {changingPassword && <ChangePasswordDialog onClose={() => setChangingPassword(false)} />}
    </div>
  );
}
