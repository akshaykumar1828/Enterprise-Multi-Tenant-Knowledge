import type { ReactNode } from "react";

import { useAuth } from "../../auth/AuthContext";
import { Icon, type IconName } from "../ui/Icon";
import { UserMenu } from "./UserMenu";

export type Section = "chat" | "documents" | "admin";

const TITLES: Record<Section, string> = { chat: "Chat", documents: "Documents", admin: "Administration" };

function NavButton({ section, icon, label, current, onSelect }: {
  section: Section; icon: IconName; label: string; current: Section; onSelect: (section: Section) => void;
}) {
  return (
    <button type="button" className="nav-item" aria-current={current === section ? "page" : undefined}
            onClick={() => onSelect(section)}>
      <Icon name={icon} /> <span>{label}</span>
    </button>
  );
}

/** Sidebar navigation + top bar. Admin appears only for admins (display only; the server enforces). */
export function AppShell({ section, onSelect, isAdmin, children }: {
  section: Section;
  onSelect: (section: Section) => void;
  isAdmin: boolean;
  children: ReactNode;
}) {
  const { user } = useAuth();
  const roleLine = user
    ? [user.role === "admin" ? "Admin" : "Employee", ...user.departments.map((d) => d.name)].join(" · ")
    : "";

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true"><Icon name="sparkle" size={18} /></span>
          <div>
            <p className="brand__name">Enterprise Knowledge</p>
            {user && <p className="brand__org">{user.tenant.name}</p>}
          </div>
        </div>
        <nav className="nav" aria-label="Main">
          <NavButton section="chat" icon="chat" label="Chat" current={section} onSelect={onSelect} />
          <NavButton section="documents" icon="documents" label="Documents" current={section} onSelect={onSelect} />
          {isAdmin && (
            <div className="nav__group">
              <p className="nav__group-label" aria-hidden="true">Administration</p>
              <NavButton section="admin" icon="admin" label="Admin" current={section} onSelect={onSelect} />
            </div>
          )}
        </nav>
        <p className="sidebar__footer" title="Your role and departments decide which documents you can see.">
          Signed in as <span className="sidebar__role">{roleLine}</span>
        </p>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <h1 className="topbar__title">{TITLES[section]}</h1>
          <UserMenu />
        </header>
        {children}
      </div>
    </div>
  );
}
