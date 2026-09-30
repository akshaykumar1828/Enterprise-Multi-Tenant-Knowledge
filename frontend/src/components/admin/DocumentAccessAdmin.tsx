import { useCallback, useEffect, useState, type FormEvent } from "react";

import { getDocumentAccess, listDepartments, listDocuments, setDocumentAccess } from "../../api/endpoints";
import type { Department, DocumentAccess, DocumentList, Visibility } from "../../api/types";
import { useAuth } from "../../auth/AuthContext";
import { useAdminErrors } from "./useAdminErrors";

const PAGE_SIZE = 20;

/** Pick a document (from the normal document list, which shows admins everything) and edit its access. */
export function DocumentAccessAdmin() {
  const { token } = useAuth();
  const { error, setError, report } = useAdminErrors();
  const [page, setPage] = useState<DocumentList | null>(null);
  const [offset, setOffset] = useState(0);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [selected, setSelected] = useState<DocumentAccess | null>(null);
  const [visibility, setVisibility] = useState<Visibility>("company");
  const [chosen, setChosen] = useState<number[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (nextOffset: number) => {
      if (!token) return;
      try {
        const [documents, allDepartments] = await Promise.all([
          listDocuments(token, PAGE_SIZE, nextOffset), listDepartments(token),
        ]);
        setPage(documents);
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

  return (
    <section aria-labelledby="access-title">
      <h2 id="access-title">Document access</h2>
      {error && <p className="alert alert--error" role="alert">{error}</p>}
      {notice && <p className="alert alert--info" role="status">{notice}</p>}

      {selected && (
        <form onSubmit={onSave} className="access-editor" aria-label={`Access for ${selected.filename}`}>
          <h3>{selected.filename}</h3>
          <fieldset>
            <legend>Who can read this document?</legend>
            <label>
              <input type="radio" name="visibility" value="company" checked={visibility === "company"}
                     onChange={() => setVisibility("company")} />
              Everyone in the company
            </label>
            <label>
              <input type="radio" name="visibility" value="departments" checked={visibility === "departments"}
                     onChange={() => setVisibility("departments")} />
              Only selected departments (and admins)
            </label>
          </fieldset>
          {visibility === "departments" && (
            <fieldset>
              <legend>Departments</legend>
              {departments.length === 0 && <p className="hint">No departments exist yet.</p>}
              {departments.map((department) => (
                <label key={department.id}>
                  <input type="checkbox" checked={chosen.includes(department.id)}
                         onChange={() => toggle(department.id)} />
                  {department.name}
                </label>
              ))}
              {chosen.length === 0 && (
                <p className="hint" role="note">With no departments selected, only administrators can read it.</p>
              )}
            </fieldset>
          )}
          <div className="admin-inline">
            <button type="submit" className="button" disabled={busy}>{busy ? "Saving…" : "Save access"}</button>
            <button type="button" className="button button--secondary" onClick={() => setSelected(null)}>Close</button>
          </div>
        </form>
      )}

      {page === null && !error && <p role="status" className="hint">Loading documents…</p>}
      {page && total === 0 && <p className="hint">No documents yet.</p>}
      {page && page.items.length > 0 && (
        <table className="documents documents--admin">
          <thead>
            <tr>
              <th scope="col">File</th>
              <th scope="col">Type</th>
              <th scope="col"><span className="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {page.items.map((document) => (
              <tr key={document.id}>
                <td className="documents__name">{document.filename}</td>
                <td>{document.source_type}</td>
                <td>
                  <button type="button" className="button button--secondary button--small"
                          aria-label={`Manage access for ${document.filename}`} onClick={() => void open(document.id)}>
                    Manage access
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {total > PAGE_SIZE && (
        <nav className="pager" aria-label="Document pages">
          <button type="button" className="button button--secondary button--small" disabled={offset === 0}
                  onClick={() => void load(Math.max(0, offset - PAGE_SIZE))}>
            Previous
          </button>
          <span className="hint">{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</span>
          <button type="button" className="button button--secondary button--small"
                  disabled={offset + PAGE_SIZE >= total} onClick={() => void load(offset + PAGE_SIZE)}>
            Next
          </button>
        </nav>
      )}
    </section>
  );
}
