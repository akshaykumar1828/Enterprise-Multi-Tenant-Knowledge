import { useEffect, useRef, useState, type FormEvent } from "react";

import { getDocumentAccess, listDepartments, setDocumentAccess } from "../../api/endpoints";
import type { Department, DocumentAccess, Visibility } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { documentTitle, documentTypeLabel } from "../../utils/documents";
import { DocumentName } from "../DocumentName";
import { Icon } from "../ui/Icon";
import { Pager } from "../ui/Pager";
import { useDocumentLibrary } from "../useDocumentLibrary";
import { useAdminErrors } from "./useAdminErrors";

const PAGE_SIZE = 20;

function accessSummary(access: DocumentAccess, departments: Department[]): string {
  if (access.visibility === "company") return "Everyone in the company";
  if (access.department_ids.length === 0) return "Administrators only";
  const names = access.department_ids.map((id) => departments.find((d) => d.id === id)?.name ?? `Department ${id}`);
  return names.join(", ");
}

/** Pick a document (from the normal document list, which shows admins everything) and edit its access.
 *  `active`: whether its tab is showing; on returning, the department list is re-read. */
export function DocumentAccessAdmin({ active = true }: { active?: boolean }) {
  const { token } = useAuth();
  const { error, setError, report } = useAdminErrors();
  const library = useDocumentLibrary(PAGE_SIZE, report);
  const { page, offset, query, setQuery, searching, matches } = library;
  const [departments, setDepartments] = useState<Department[]>([]);
  const [selected, setSelected] = useState<DocumentAccess | null>(null);
  const [visibility, setVisibility] = useState<Visibility>("company");
  const [chosen, setChosen] = useState<number[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    listDepartments(token).then(setDepartments).catch(report);
  }, [token, report]);

  const wasActive = useRef(active);
  useEffect(() => {
    if (active && !wasActive.current && token) listDepartments(token).then(setDepartments).catch(report);
    wasActive.current = active;
  }, [active, token, report]);

  function show(access: DocumentAccess) {
    setSelected(access);
    setVisibility(access.visibility);
    setChosen(access.department_ids);
  }

  async function open(documentId: number) {
    if (!token) return;
    setError(null);
    setNotice(null);
    setSelected(null);
    try {
      show(await getDocumentAccess(token, documentId));
    } catch (caught) {
      report(caught);
    }
  }

  async function onSave(event: FormEvent) {
    event.preventDefault();
    if (!token || !selected) return;
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      const saved = await setDocumentAccess(token, selected.id, visibility, chosen);
      show(saved);
      setNotice(`Saved access for ${saved.filename}.`);
    } catch (caught) {
      report(caught);
    } finally {
      setBusy(false);
    }
  }

  const toggle = (id: number) =>
    setChosen((current) => (current.includes(id) ? current.filter((x) => x !== id) : [...current, id]));
  const total = page?.total ?? 0;
  const rows = matches ?? page?.items ?? [];

  return (
    <section aria-labelledby="access-title" className="admin-section">
      <div className="admin-section__header">
        <h2 id="access-title" className="section-title">Document access</h2>
        <div className="search">
          <label htmlFor="access-search" className="visually-hidden">Search documents</label>
          <Icon name="search" size={16} />
          <input id="access-search" type="search" placeholder="Find a document" value={query}
                 onChange={(event) => setQuery(event.target.value)} />
        </div>
      </div>
      {error && <p className="notice notice--error" role="alert">{error}</p>}
      {notice && <p className="notice notice--success" role="status">{notice}</p>}

      {selected && (
        <form onSubmit={onSave} className="panel access-editor" aria-label={`Access for ${selected.filename}`}>
          <div>
            <h3 className="access-editor__title">{documentTitle(selected.filename)}</h3>
            <p className="muted small">{selected.filename} · currently: {accessSummary(selected, departments)}</p>
          </div>
          <fieldset>
            <legend>Who can read this document?</legend>
            <label className="choice">
              <input type="radio" name="visibility" value="company" checked={visibility === "company"}
                     onChange={() => setVisibility("company")} />
              Everyone in the company
            </label>
            <label className="choice">
              <input type="radio" name="visibility" value="departments" checked={visibility === "departments"}
                     onChange={() => setVisibility("departments")} />
              Only selected departments (and admins)
            </label>
          </fieldset>
          {visibility === "departments" && (
            <fieldset>
              <legend>Departments</legend>
              {departments.length === 0 && <p className="muted small">No departments exist yet.</p>}
              <div className="choice-grid">
                {departments.map((department) => (
                  <label key={department.id} className="choice">
                    <input type="checkbox" checked={chosen.includes(department.id)}
                           onChange={() => toggle(department.id)} />
                    {department.name}
                  </label>
                ))}
              </div>
              {chosen.length === 0 && (
                <p className="field__hint" role="note">With no departments selected, only administrators can read it.</p>
              )}
            </fieldset>
          )}
          <div className="inline-form__actions">
            <button type="submit" className="button" disabled={busy}>{busy ? "Saving…" : "Save access"}</button>
            <button type="button" className="button button--ghost" onClick={() => setSelected(null)}>Close</button>
          </div>
        </form>
      )}

      {page === null && !error && <p role="status" className="muted">Loading documents…</p>}
      {searching && <p role="status" className="muted">Searching all documents…</p>}
      {page && total === 0 && <div className="empty"><p className="empty__title">No documents yet</p></div>}
      {matches && matches.length === 0 && <div className="empty"><p className="empty__title">No documents match “{query.trim()}”</p></div>}
      {rows.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Document</th>
              <th scope="col" className="table__optional">Type</th>
              <th scope="col"><span className="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((document) => (
              <tr key={document.id} className={selected?.id === document.id ? "is-selected" : undefined}>
                <td><DocumentName document={document} /></td>
                <td className="table__optional">{documentTypeLabel(document.source_type, document.filename)}</td>
                <td className="table__actions">
                  <button type="button" className="button button--ghost button--small"
                          aria-label={`Manage access for ${document.filename}`} onClick={() => void open(document.id)}>
                    Manage access
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!matches && (
        <Pager offset={offset} pageSize={PAGE_SIZE} total={total} label="Document pages"
               onPage={(next) => void library.load(next)} />
      )}
    </section>
  );
}
