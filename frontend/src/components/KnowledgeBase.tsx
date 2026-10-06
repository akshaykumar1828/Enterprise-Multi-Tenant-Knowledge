import { useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { deleteDocument, uploadDocument } from "../api/endpoints";
import type { KnowledgeDocument } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { documentTypeLabel, formatDate } from "../utils/documents";
import { DocumentName } from "./DocumentName";
import { useConfirm } from "./ui/ConfirmDialog";
import { Icon } from "./ui/Icon";
import { Pager } from "./ui/Pager";
import { useDocumentLibrary } from "./useDocumentLibrary";

// Quick feedback in the browser; the server validates again and has the final say.
export const ACCEPTED_EXTENSIONS = [".pdf", ".txt", ".md", ".markdown"];
export const MAX_UPLOAD_MB = 10;
const PAGE_SIZE = 20;

function checkFile(file: File): string | null {
  const name = file.name.toLowerCase();
  if (!ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return "Only PDF, TXT and Markdown files can be uploaded.";
  }
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) return `Files can be at most ${MAX_UPLOAD_MB} MB.`;
  if (file.size === 0) return "The file is empty.";
  return null;
}

const message = (error: unknown) => (error instanceof ApiError ? error.message : "Something went wrong. Please try again.");

/** The Documents page: what the user can search, plus upload and (for their uploads) delete. */
export function KnowledgeBase() {
  const { token } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const library = useDocumentLibrary(PAGE_SIZE, (caught) => setError(message(caught)));
  const { page, offset, loading, refresh, query, setQuery, searching, matches } = library;
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const { confirm, dialog } = useConfirm();

  async function onUpload(event: FormEvent) {
    event.preventDefault();
    if (!token || !file) return;
    setError(null);
    setNotice(null);
    const problem = checkFile(file);
    if (problem) {
      setError(problem);
      return;
    }
    setUploading(true);
    try {
      const document = await uploadDocument(token, file);
      setNotice(`Uploaded ${document.filename}. It can now be used to answer questions in Chat.`);
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await refresh(0);
    } catch (caught) {
      setError(message(caught));
    } finally {
      setUploading(false);
    }
  }

  async function onDelete(document: KnowledgeDocument) {
    if (!token) return;
    const confirmed = await confirm({
      title: "Delete this document?",
      message: <p><strong>{document.filename}</strong> will be removed and will no longer be used to answer questions. This cannot be undone.</p>,
      confirmLabel: "Delete document",
      danger: true,
    });
    if (!confirmed) return;
    setError(null);
    setNotice(null);
    setDeletingId(document.id);
    try {
      await deleteDocument(token, document.id);
      setNotice(`Deleted ${document.filename}.`);
      // Stay on the same page unless it just became empty.
      const lastOnPage = page !== null && page.items.length === 1 && offset > 0;
      await refresh(lastOnPage ? Math.max(0, offset - PAGE_SIZE) : offset);
    } catch (caught) {
      setError(message(caught));
      // Gone, or no longer readable (e.g. its access changed): show the server's current list.
      if (caught instanceof ApiError && caught.status === 404) await refresh(offset);
    } finally {
      setDeletingId(null);
    }
  }

  const total = page?.total ?? 0;
  const rows = matches ?? page?.items ?? [];
  return (
    <main className="page">
      <div className="page__intro">
        <p>
          These are the documents you have access to. Chat answers questions using only these documents. Documents
          shared with specific departments appear here only if you belong to one of them.
        </p>
      </div>

      <section className="panel" aria-labelledby="upload-title">
        <div className="panel__header">
          <h2 id="upload-title" className="panel__title"><Icon name="upload" size={16} /> Add a document</h2>
          <p className="panel__hint">
            PDF, TXT or Markdown, up to {MAX_UPLOAD_MB} MB. New uploads are visible to everyone in your organization;
            an administrator can limit them to departments.
          </p>
        </div>
        <form onSubmit={onUpload} className="upload">
          <label htmlFor="document-file" className="visually-hidden">Document file</label>
          <input
            id="document-file"
            ref={fileInput}
            type="file"
            accept={ACCEPTED_EXTENSIONS.join(",")}
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              setError(null);
            }}
          />
          <button type="submit" className="button" disabled={!file || uploading}>
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </form>
        {uploading && <p role="status" className="muted small">Processing the document. This can take a moment for large files…</p>}
      </section>

      {error && <p className="notice notice--error" role="alert">{error}</p>}
      {notice && <p className="notice notice--success" role="status">{notice}</p>}

      <section aria-labelledby="documents-title" className="library">
        <div className="library__header">
          <h2 id="documents-title" className="section-title">
            Documents {page && <span className="count">({total})</span>}
          </h2>
          <div className="search">
            <label htmlFor="document-search" className="visually-hidden">Search documents</label>
            <Icon name="search" size={16} />
            <input id="document-search" type="search" placeholder="Search titles, descriptions or types" value={query}
                   onChange={(event) => setQuery(event.target.value)} />
          </div>
        </div>

        {loading && !page && <p role="status" className="muted">Loading documents…</p>}
        {searching && <p role="status" className="muted">Searching all documents…</p>}
        {page && total === 0 && (
          <div className="empty">
            <p className="empty__title">No documents yet</p>
            <p className="muted">Upload one above to start asking questions about it.</p>
          </div>
        )}
        {matches && matches.length === 0 && (
          <div className="empty"><p className="empty__title">No documents match “{query.trim()}”</p></div>
        )}
        {matches && matches.length > 0 && (
          <p className="muted small">{matches.length} matching {matches.length === 1 ? "document" : "documents"}</p>
        )}

        {rows.length > 0 && (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Document</th>
                <th scope="col" className="table__optional">Type</th>
                <th scope="col" className="table__optional">Added</th>
                <th scope="col"><span className="visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((document) => (
                <tr key={document.id}>
                  <td><DocumentName document={document} /></td>
                  <td className="table__optional">{documentTypeLabel(document.source_type, document.filename)}</td>
                  <td className="table__optional">{formatDate(document.ingested_at)}</td>
                  <td className="table__actions">
                    {document.deletable ? (
                      <button
                        type="button"
                        className="button button--danger-ghost button--small"
                        disabled={deletingId !== null}
                        aria-label={`Delete ${document.filename}`}
                        onClick={() => void onDelete(document)}
                      >
                        {deletingId === document.id ? "Deleting…" : "Delete"}
                      </button>
                    ) : (
                      <span className="tag" title="Part of the company library; managed by your administrators">Managed</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {!matches && (
          <Pager offset={offset} pageSize={PAGE_SIZE} total={total} disabled={loading} label="Document pages"
                 onPage={(next) => void library.load(next)} />
        )}
      </section>
      {dialog}
    </main>
  );
}
