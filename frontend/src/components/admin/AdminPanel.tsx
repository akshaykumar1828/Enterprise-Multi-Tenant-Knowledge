import { useRef, useState, type KeyboardEvent } from "react";

import { DepartmentsAdmin } from "./DepartmentsAdmin";
import { DocumentAccessAdmin } from "./DocumentAccessAdmin";
import { UsersAdmin } from "./UsersAdmin";

type View = "departments" | "users" | "access";
const VIEWS: Array<{ key: View; title: string; description: string }> = [
  { key: "departments", title: "Departments", description: "Groups that documents can be shared with." },
  { key: "users", title: "Users", description: "Roles and department memberships." },
  { key: "access", title: "Document Access", description: "Who can read each document." },
];

/**
 * Company administration. Shown only to users whose /auth/me role is admin, but
 * every call is still checked by the server (403 → permission message).
 *
 * Each section mounts the first time its tab is opened and then stays mounted (hidden
 * while another tab is active), so switching tabs keeps its state: an open editor, the
 * search text, the page, messages. Nothing is requested before a tab is first opened.
 */
export function AdminPanel() {
  const [view, setView] = useState<View>("departments");
  const [opened, setOpened] = useState<Set<View>>(() => new Set(["departments"]));
  const tabs = useRef<Array<HTMLButtonElement | null>>([]);

  function select(next: View) {
    setView(next);
    setOpened((current) => (current.has(next) ? current : new Set(current).add(next)));
  }

  // Arrow keys (and Home/End) move between tabs (WAI-ARIA tabs pattern, automatic activation).
  function onKeyDown(event: KeyboardEvent, index: number) {
    const last = VIEWS.length - 1;
    const next = event.key === "ArrowRight" ? (index + 1) % VIEWS.length
      : event.key === "ArrowLeft" ? (index - 1 + VIEWS.length) % VIEWS.length
      : event.key === "Home" ? 0 : event.key === "End" ? last : null;
    if (next === null) return;
    event.preventDefault();
    select(VIEWS[next].key);
    tabs.current[next]?.focus();
  }

  return (
    <main className="page page--admin">
      <div className="page__intro">
        <p>Manage departments, people and document access for your organization. Changes take effect on each person's next request.</p>
      </div>
      <div className="tabs" role="tablist" aria-label="Administration sections">
        {VIEWS.map((item, index) => (
          <button key={item.key} ref={(el) => { tabs.current[index] = el; }} type="button" role="tab"
                  id={`admin-tab-${item.key}`} aria-controls={`admin-panel-${item.key}`} aria-selected={view === item.key}
                  tabIndex={view === item.key ? 0 : -1} className="tabs__tab"
                  onClick={() => select(item.key)} onKeyDown={(event) => onKeyDown(event, index)}>
            {item.title}
          </button>
        ))}
      </div>
      {VIEWS.map((item) => (
        <div key={item.key} id={`admin-panel-${item.key}`} role="tabpanel" aria-labelledby={`admin-tab-${item.key}`}
             hidden={view !== item.key} className="tabs__panel">
          <p className="muted tabs__description">{item.description}</p>
          {item.key === "departments" && opened.has("departments") && <DepartmentsAdmin />}
          {item.key === "users" && opened.has("users") && <UsersAdmin active={view === "users"} />}
          {item.key === "access" && opened.has("access") && <DocumentAccessAdmin active={view === "access"} />}
        </div>
      ))}
    </main>
  );
}
