import { useState } from "react";

import { DepartmentsAdmin } from "./DepartmentsAdmin";
import { DocumentAccessAdmin } from "./DocumentAccessAdmin";
import { UsersAdmin } from "./UsersAdmin";

type View = "departments" | "users" | "access";
const VIEWS: Array<[View, string]> = [["departments", "Departments"], ["users", "Users"], ["access", "Document access"]];

/**
 * Company administration. Shown only to users whose /auth/me role is admin, but
 * every call is still checked by the server (403 → permission message).
 */
export function AdminPanel() {
  const [view, setView] = useState<View>("departments");
  return (
    <main className="knowledge admin">
      <nav className="admin-views" aria-label="Administration">
        {VIEWS.map(([key, title]) => (
          <button key={key} type="button" className="button button--secondary button--small"
                  aria-current={view === key ? "page" : undefined} onClick={() => setView(key)}>
            {title}
          </button>
        ))}
      </nav>
      {view === "departments" && <DepartmentsAdmin />}
      {view === "users" && <UsersAdmin />}
      {view === "access" && <DocumentAccessAdmin />}
    </main>
  );
}
