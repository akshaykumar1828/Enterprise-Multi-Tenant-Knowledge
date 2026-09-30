import { useCallback, useEffect, useState, type FormEvent } from "react";

import { createDepartment, deleteDepartment, listDepartments, updateDepartment } from "../../api/endpoints";
import type { Department } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { useAdminErrors } from "./useAdminErrors";

export function DepartmentsAdmin() {
  const { token } = useAuth();
  const { error, setError, report } = useAdminErrors();
  const [departments, setDepartments] = useState<Department[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [editing, setEditing] = useState<{ id: number; name: string } | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      setDepartments(await listDepartments(token));
    } catch (caught) {
      report(caught);
    }
  }, [token, report]);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(action: () => Promise<unknown>, success: string) {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      await action();
      setNotice(success);
      await load();
      return true;
    } catch (caught) {
      report(caught);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function onCreate(event: FormEvent) {
    event.preventDefault();
    if (!token) return;
    if (await run(() => createDepartment(token, slug.trim(), name.trim()), `Created ${name.trim()}.`)) {
      setName("");
      setSlug("");
    }
  }

  async function onRename(event: FormEvent) {
    event.preventDefault();
    if (!token || !editing) return;
    if (await run(() => updateDepartment(token, editing.id, { name: editing.name.trim() }), "Department renamed.")) {
      setEditing(null);
    }
  }

  async function onDelete(department: Department) {
    if (!token || !window.confirm(`Delete the department "${department.name}"? Its members will be removed from it.`)) {
      return;
    }
    await run(() => deleteDepartment(token, department.id), `Deleted ${department.name}.`);
  }

  return (
    <section aria-labelledby="departments-title">
      <h2 id="departments-title">Departments</h2>
      <form onSubmit={onCreate} className="admin-form" aria-label="New department">
        <label>
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} required maxLength={100} />
        </label>
        <label>
          Slug
          <input value={slug} onChange={(event) => setSlug(event.target.value)} required maxLength={63}
                 placeholder="e.g. finance" />
        </label>
        <button type="submit" className="button" disabled={busy || !name.trim() || !slug.trim()}>
          Create department
        </button>
      </form>
      <p className="hint">Slugs use lowercase letters, digits and "-".</p>
      {error && <p className="alert alert--error" role="alert">{error}</p>}
      {notice && <p className="alert alert--info" role="status">{notice}</p>}

      {departments === null && !error && <p role="status" className="hint">Loading departments…</p>}
      {departments?.length === 0 && <p className="hint">No departments yet.</p>}
      {departments && departments.length > 0 && (
        <table className="documents documents--admin">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Slug</th>
              <th scope="col">Members</th>
              <th scope="col">Documents</th>
              <th scope="col"><span className="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {departments.map((department) => (
              <tr key={department.id}>
                <td>
                  {editing?.id === department.id ? (
                    <form onSubmit={onRename} className="admin-inline">
                      <label className="visually-hidden" htmlFor={`rename-${department.id}`}>New name</label>
                      <input id={`rename-${department.id}`} value={editing.name} required maxLength={100}
                             onChange={(event) => setEditing({ id: department.id, name: event.target.value })} />
                      <button type="submit" className="button button--small" disabled={busy}>Save</button>
                      <button type="button" className="button button--secondary button--small"
                              onClick={() => setEditing(null)}>Cancel</button>
                    </form>
                  ) : (
                    department.name
                  )}
                </td>
                <td>{department.slug}</td>
                <td>{department.member_count}</td>
                <td>{department.document_count}</td>
                <td className="admin-actions">
                  <button type="button" className="button button--secondary button--small" disabled={busy}
                          aria-label={`Rename ${department.name}`}
                          onClick={() => setEditing({ id: department.id, name: department.name })}>
                    Rename
                  </button>
                  <button type="button" className="button button--secondary button--small" disabled={busy}
                          aria-label={`Delete ${department.name}`} onClick={() => void onDelete(department)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
