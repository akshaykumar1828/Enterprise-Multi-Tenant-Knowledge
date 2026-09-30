import { useCallback, useEffect, useState } from "react";

import {
  addUserToDepartment,
  listCompanyUsers,
  listDepartments,
  removeUserFromDepartment,
  setUserRole,
} from "../../api/endpoints";
import type { CompanyUser, CompanyUserList, Department, Role } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { useAdminErrors } from "./useAdminErrors";

const PAGE_SIZE = 50;

export function UsersAdmin() {
  const { token, user: me, refreshUser } = useAuth();
  const { error, setError, report } = useAdminErrors();
  const [page, setPage] = useState<CompanyUserList | null>(null);
  const [offset, setOffset] = useState(0);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [adding, setAdding] = useState<Record<number, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (nextOffset: number) => {
      if (!token) return;
      try {
        const [users, allDepartments] = await Promise.all([
          listCompanyUsers(token, PAGE_SIZE, nextOffset), listDepartments(token),
        ]);
        setPage(users);
        setOffset(nextOffset);
        setDepartments(allDepartments);
      } catch (caught) {
        report(caught);
      }
    },
    [token, report],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

  const departmentName = (id: number) => departments.find((d) => d.id === id)?.name ?? `Department ${id}`;
  const label = (user: CompanyUser) => user.display_name ?? user.email;

  async function run(action: () => Promise<unknown>, success: string, affectsMe: boolean) {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      await action();
      setNotice(success);
      // My own role or departments changed: re-read them (e.g. the admin section may go away).
      if (affectsMe) await refreshUser();
      await load(offset);
    } catch (caught) {
      report(caught);
    } finally {
      setBusy(false);
    }
  }

  function onRole(user: CompanyUser, role: Role) {
    if (!token || role === user.role) return;
    void run(() => setUserRole(token, user.id, role), `${label(user)} is now ${role === "admin" ? "an admin" : "an employee"}.`,
             user.id === me?.id);
  }

  function onAdd(user: CompanyUser) {
    const departmentId = Number(adding[user.id]);
    if (!token || !departmentId) return;
    setAdding((current) => ({ ...current, [user.id]: "" }));
    void run(() => addUserToDepartment(token, user.id, departmentId),
             `Added ${label(user)} to ${departmentName(departmentId)}.`, user.id === me?.id);
  }

  function onRemove(user: CompanyUser, departmentId: number) {
    if (!token) return;
    void run(() => removeUserFromDepartment(token, user.id, departmentId),
             `Removed ${label(user)} from ${departmentName(departmentId)}.`, user.id === me?.id);
  }

  const total = page?.total ?? 0;
  return (
    <section aria-labelledby="users-title">
      <h2 id="users-title">Users {page && <span className="count">({total})</span>}</h2>
      {error && <p className="alert alert--error" role="alert">{error}</p>}
      {notice && <p className="alert alert--info" role="status">{notice}</p>}
      {page === null && !error && <p role="status" className="hint">Loading users…</p>}
      {page && page.items.length > 0 && (
        <table className="documents documents--admin">
          <thead>
            <tr>
              <th scope="col">User</th>
              <th scope="col">Role</th>
              <th scope="col">Departments</th>
            </tr>
          </thead>
          <tbody>
            {page.items.map((user) => {
              const available = departments.filter((d) => !user.department_ids.includes(d.id));
              return (
                <tr key={user.id}>
                  <td className="documents__name">
                    {label(user)}
                    {user.display_name && <div className="hint">{user.email}</div>}
                  </td>
                  <td>
                    <label className="visually-hidden" htmlFor={`role-${user.id}`}>Role for {label(user)}</label>
                    <select id={`role-${user.id}`} value={user.role} disabled={busy}
                            onChange={(event) => onRole(user, event.target.value as Role)}>
                      <option value="employee">Employee</option>
                      <option value="admin">Admin</option>
                    </select>
                  </td>
                  <td>
                    <ul className="chips" aria-label={`Departments of ${label(user)}`}>
                      {user.department_ids.map((id) => (
                        <li key={id} className="chip">
                          {departmentName(id)}
                          <button type="button" className="chip__remove" disabled={busy}
                                  aria-label={`Remove ${label(user)} from ${departmentName(id)}`}
                                  onClick={() => onRemove(user, id)}>
                            ×
                          </button>
                        </li>
                      ))}
                      {user.department_ids.length === 0 && <li className="hint">None</li>}
                    </ul>
                    {available.length > 0 && (
                      <div className="admin-inline">
                        <label className="visually-hidden" htmlFor={`add-${user.id}`}>
                          Add {label(user)} to a department
                        </label>
                        <select id={`add-${user.id}`} value={adding[user.id] ?? ""} disabled={busy}
                                onChange={(event) => setAdding((current) => ({ ...current, [user.id]: event.target.value }))}>
                          <option value="">Add to department…</option>
                          {available.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                        </select>
                        <button type="button" className="button button--small" disabled={busy || !adding[user.id]}
                                aria-label={`Add ${label(user)} to the selected department`} onClick={() => onAdd(user)}>
                          Add
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {total > PAGE_SIZE && (
        <nav className="pager" aria-label="User pages">
          <button type="button" className="button button--secondary button--small" disabled={offset === 0 || busy}
                  onClick={() => void load(Math.max(0, offset - PAGE_SIZE))}>
            Previous
          </button>
          <span className="hint">{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</span>
          <button type="button" className="button button--secondary button--small"
                  disabled={offset + PAGE_SIZE >= total || busy} onClick={() => void load(offset + PAGE_SIZE)}>
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
