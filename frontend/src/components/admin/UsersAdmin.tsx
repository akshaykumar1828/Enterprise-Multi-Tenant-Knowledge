import { useCallback, useEffect, useRef, useState } from "react";

import {
  addUserToDepartment,
  createEmployee,
  deleteUser,
  listCompanyUsers,
  listDepartments,
  removeUserFromDepartment,
  setUserRole,
} from "../../api/endpoints";
import type { CompanyUser, CompanyUserList, Department, Role } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { initials } from "../../utils/documents";
import { useConfirm } from "../ui/ConfirmDialog";
import { Icon } from "../ui/Icon";
import { Pager } from "../ui/Pager";
import { AddEmployeeForm, type NewEmployee } from "./AddEmployeeForm";
import { useAdminErrors } from "./useAdminErrors";

const PAGE_SIZE = 50;

/** `active`: whether its tab is showing. On returning to the tab, users and departments are re-read
 *  (a department may have been created or renamed in the Departments tab meanwhile). */
export function UsersAdmin({ active = true }: { active?: boolean }) {
  const { token, user: me, refreshUser } = useAuth();
  const { error, setError, report } = useAdminErrors();
  const { confirm, dialog } = useConfirm();
  const [page, setPage] = useState<CompanyUserList | null>(null);
  const [offset, setOffset] = useState(0);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [adding, setAdding] = useState<Record<number, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);

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

  const wasActive = useRef(active);
  useEffect(() => {
    if (active && !wasActive.current) void load(offset);
    wasActive.current = active;
  }, [active, load, offset]);

  const departmentName = (id: number) => departments.find((d) => d.id === id)?.name ?? `Department ${id}`;
  const label = (user: CompanyUser) => user.display_name ?? user.email;

  async function run(action: () => Promise<unknown>, success: string, affectsMe: boolean): Promise<boolean> {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      await action();
      setNotice(success);
      // My own role or departments changed: re-read them (e.g. the admin section may go away).
      if (affectsMe) await refreshUser();
      await load(offset);
      return true;
    } catch (caught) {
      report(caught);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(user: CompanyUser) {
    if (!token) return;
    const confirmed = await confirm({
      title: `Delete ${label(user)}?`,
      message: (
        <>
          <p><strong>{user.email}</strong> will no longer be able to log in, and their department memberships are removed.
            Documents they uploaded stay in the library.</p>
          <p>This cannot be undone.</p>
        </>
      ),
      confirmLabel: "Delete user",
      danger: true,
    });
    if (!confirmed) return;
    // A deleted user leaves the list; step back a page if it was the last one shown.
    const lastOnPage = page !== null && page.items.length === 1 && offset > 0;
    if (await run(() => deleteUser(token, user.id), `Deleted ${label(user)}.`, false) && lastOnPage) {
      await load(Math.max(0, offset - PAGE_SIZE));
    }
  }

  async function onCreate(employee: NewEmployee): Promise<boolean> {
    if (!token) return false;
    const who = employee.display_name ?? employee.email;
    const ok = await run(() => createEmployee(token, employee),
                         `Added ${who}. They can now log in with ${employee.email} and the initial password.`, false);
    if (ok) setCreating(false);
    return ok;
  }

  async function onRole(user: CompanyUser, role: Role) {
    if (!token || role === user.role) return;
    const isMe = user.id === me?.id;
    if (isMe && role === "employee") {
      const confirmed = await confirm({
        title: "Remove your own admin access?",
        message: <p>You will immediately lose access to Administration. Another admin will have to restore it.</p>,
        confirmLabel: "Remove my admin access",
        danger: true,
      });
      if (!confirmed) return;
    }
    await run(() => setUserRole(token, user.id, role), `${label(user)} is now ${role === "admin" ? "an admin" : "an employee"}.`, isMe);
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
    <section aria-labelledby="users-title" className="admin-section">
      <div className="admin-section__header">
        <h2 id="users-title" className="section-title">Users {page && <span className="count">({total})</span>}</h2>
        {!creating && (
          <button type="button" className="button button--small" onClick={() => { setCreating(true); setNotice(null); }}>
            <Icon name="plus" size={14} /> Add employee
          </button>
        )}
      </div>
      {creating && (
        <AddEmployeeForm departments={departments} busy={busy} onSubmit={onCreate} onCancel={() => setCreating(false)} />
      )}
      <p className="muted small">
        Admins can manage the organization and read every document. Employees read company-wide documents plus
        those shared with their departments.
      </p>
      {error && <p className="notice notice--error" role="alert">{error}</p>}
      {notice && <p className="notice notice--success" role="status">{notice}</p>}
      {page === null && !error && <p role="status" className="muted">Loading users…</p>}
      {page && page.items.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Person</th>
              <th scope="col">Role</th>
              <th scope="col">Departments</th>
              <th scope="col"><span className="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {page.items.map((user) => {
              const available = departments.filter((d) => !user.department_ids.includes(d.id));
              return (
                <tr key={user.id}>
                  <td>
                    <div className="person">
                      <span className="avatar" aria-hidden="true">{initials(label(user))}</span>
                      <div>
                        <span className="person__name">{label(user)}</span>
                        {user.display_name && <span className="person__email">{user.email}</span>}
                        {user.id === me?.id && <span className="tag tag--subtle">You</span>}
                      </div>
                    </div>
                  </td>
                  <td>
                    <label className="visually-hidden" htmlFor={`role-${user.id}`}>Role for {label(user)}</label>
                    <select id={`role-${user.id}`} value={user.role} disabled={busy}
                            onChange={(event) => void onRole(user, event.target.value as Role)}>
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
                      {user.department_ids.length === 0 && <li className="muted small">No departments</li>}
                    </ul>
                    {available.length > 0 && (
                      <div className="row-form row-form--compact">
                        <label className="visually-hidden" htmlFor={`add-${user.id}`}>
                          Add {label(user)} to a department
                        </label>
                        <select id={`add-${user.id}`} value={adding[user.id] ?? ""} disabled={busy}
                                onChange={(event) => setAdding((current) => ({ ...current, [user.id]: event.target.value }))}>
                          <option value="">Add to department…</option>
                          {available.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
                        </select>
                        <button type="button" className="button button--ghost button--small" disabled={busy || !adding[user.id]}
                                aria-label={`Add ${label(user)} to the selected department`} onClick={() => onAdd(user)}>
                          Add
                        </button>
                      </div>
                    )}
                  </td>
                  <td className="table__actions">
                    {user.id !== me?.id && (
                      <button type="button" className="button button--danger-ghost button--small" disabled={busy}
                              aria-label={`Delete ${label(user)}`} onClick={() => void onDelete(user)}>
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <Pager offset={offset} pageSize={PAGE_SIZE} total={total} disabled={busy} label="User pages"
             onPage={(next) => void load(next)} />
      {dialog}
    </section>
  );
}
