import { useCallback, useEffect, useState, type FormEvent } from "react";

import { createDepartment, deleteDepartment, listDepartments, updateDepartment } from "../../api/endpoints";
import type { Department } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { useConfirm } from "../ui/ConfirmDialog";
import { Icon } from "../ui/Icon";
import { useAdminErrors } from "./useAdminErrors";

/** "Legal & Compliance" -> "legal-compliance" (a suggestion; the admin can edit it). */
const suggestSlug = (name: string) =>
  name.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);

export function DepartmentsAdmin() {
  const { token } = useAuth();
  const { error, setError, report } = useAdminErrors();
  const { confirm, dialog } = useConfirm();
  const [departments, setDepartments] = useState<Department[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
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
      setSlugEdited(false);
      setCreating(false);
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
    if (!token) return;
    const confirmed = await confirm({
      title: `Delete ${department.name}?`,
      message: (
        <>
          <p>Its {department.member_count} member(s) will be removed from the department. This cannot be undone.</p>
          {department.document_count > 0 && (
            <p>It is still assigned to {department.document_count} document(s); the server will refuse until those documents' access is changed.</p>
          )}
        </>
      ),
      confirmLabel: "Delete department",
      danger: true,
    });
    if (confirmed) await run(() => deleteDepartment(token, department.id), `Deleted ${department.name}.`);
  }

  return (
    <section aria-labelledby="departments-title" className="admin-section">
      <div className="admin-section__header">
        <h2 id="departments-title" className="section-title">Departments</h2>
        {!creating && (
          <button type="button" className="button button--small" onClick={() => setCreating(true)}>
            <Icon name="plus" size={14} /> New department
          </button>
        )}
      </div>

      {creating && (
        <form onSubmit={onCreate} className="panel inline-form" aria-label="New department">
          <div className="field">
            <label htmlFor="department-name">Name</label>
            <input id="department-name" value={name} required maxLength={100} autoFocus
                   onChange={(event) => {
                     setName(event.target.value);
                     if (!slugEdited) setSlug(suggestSlug(event.target.value));
                   }} />
          </div>
          <div className="field">
            <label htmlFor="department-slug">Slug</label>
            <input id="department-slug" value={slug} required maxLength={63} placeholder="e.g. finance"
                   aria-describedby="slug-hint"
                   onChange={(event) => {
                     setSlug(event.target.value);
                     setSlugEdited(true);
                   }} />
            <p id="slug-hint" className="field__hint">Lowercase letters, digits and "-". Used as a stable identifier.</p>
          </div>
          <div className="inline-form__actions">
            <button type="submit" className="button" disabled={busy || !name.trim() || !slug.trim()}>Create department</button>
            <button type="button" className="button button--ghost" onClick={() => setCreating(false)}>Cancel</button>
          </div>
        </form>
      )}

      {error && <p className="notice notice--error" role="alert">{error}</p>}
      {notice && <p className="notice notice--success" role="status">{notice}</p>}

      {departments === null && !error && <p role="status" className="muted">Loading departments…</p>}
      {departments?.length === 0 && (
        <div className="empty">
          <p className="empty__title">No departments yet</p>
          <p className="muted">Create departments to share documents with specific groups of people.</p>
        </div>
      )}
      {departments && departments.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col" className="table__optional">Slug</th>
              <th scope="col" className="table__number">Members</th>
              <th scope="col" className="table__number">Documents</th>
              <th scope="col"><span className="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {departments.map((department) => (
              <tr key={department.id}>
                <td>
                  {editing?.id === department.id ? (
                    <form onSubmit={onRename} className="row-form">
                      <label className="visually-hidden" htmlFor={`rename-${department.id}`}>New name</label>
                      <input id={`rename-${department.id}`} value={editing.name} required maxLength={100} autoFocus
                             onChange={(event) => setEditing({ id: department.id, name: event.target.value })} />
                      <button type="submit" className="button button--small" disabled={busy}>Save</button>
                      <button type="button" className="button button--ghost button--small"
                              onClick={() => setEditing(null)}>Cancel</button>
                    </form>
                  ) : (
                    department.name
                  )}
                </td>
                <td className="table__optional muted">{department.slug}</td>
                <td className="table__number">{department.member_count}</td>
                <td className="table__number">{department.document_count}</td>
                <td className="table__actions">
                  <button type="button" className="button button--ghost button--small" disabled={busy}
                          aria-label={`Rename ${department.name}`}
                          onClick={() => setEditing({ id: department.id, name: department.name })}>
                    Rename
                  </button>
                  <button type="button" className="button button--danger-ghost button--small" disabled={busy}
                          aria-label={`Delete ${department.name}`} onClick={() => void onDelete(department)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {dialog}
    </section>
  );
}
