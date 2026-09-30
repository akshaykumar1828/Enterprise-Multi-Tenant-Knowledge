import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { deleteDocument, listDocuments, uploadDocument } from "../api/endpoints";
import type { DocumentList } from "../api/types";
import { useAuth } from "../auth/AuthContext";

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

export function KnowledgeBase() {
  const { token } = useAuth();
  const [page, setPage] = useState<DocumentList | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(
    async (nextOffset: number) => {
      if (!token) return;
      setLoading(true);
      try {
        setPage(await listDocuments(token, PAGE_SIZE, nextOffset));
        setOffset(nextOffset);
      } catch (caught) {
        setError(message(caught));
      } finally {
        setLoading(false);
      }
    },
    [token],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

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
      setNotice(`Uploaded ${document.filename} (${document.chunk_count} chunks). It is now searchable in chat.`);
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await load(0);
    } catch (caught) {
      setError(message(caught));
    } finally {
      setUploading(false);
    }
  }

  async function onDelete(id: number, filename: string) {
    if (!token || !window.confirm(`Delete "${filename}"? It will no longer be used to answer questions.`)) return;
    setError(null);
    setNotice(null);
    setDeletingId(id);
    try {
      await deleteDocument(token, id);
      setNotice(`Deleted ${filename}.`);
      // Stay on the same page unless it just became empty.
      const lastOnPage = page !== null && page.items.length === 1 && offset > 0;
      await load(lastOnPage ? Math.max(0, offset - PAGE_SIZE) : offset);
    } catch (caught) {
      setError(message(caught));
      // Gone, or no longer readable (e.g. its access changed): show the server's current list.
      if (caught instanceof ApiError && caught.status === 404) await load(offset);
    } finally {
      setDeletingId(null);
    }
  }

  const total = page?.total ?? 0;
  return (
    <main className="knowledge">
      <section className="knowledge__upload" aria-labelledby="upload-title">
        <h2 id="upload-title">Upload a document</h2>
        <form onSubmit={onUpload} className="upload-form">
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
        <p className="hint">
          PDF, TXT or Markdown, up to {MAX_UPLOAD_MB} MB. Everyone in your organization can search it; an
          administrator can limit it to departments.
        </p>
        {uploading && <p role="status" className="hint">Reading, indexing and embedding the document…</p>}
        {error && <p className="alert alert--error" role="alert">{error}</p>}
        {notice && <p className="alert alert--info" role="status">{notice}</p>}
      </section>

      <section aria-labelledby="documents-title">
        <h2 id="documents-title">Documents {page && <span className="count">({total})</span>}</h2>
        {loading && !page && <p role="status" className="hint">Loading documents…</p>}
        {page && total === 0 && <p className="hint">No documents yet. Upload one to start asking questions about it.</p>}
        {page && page.items.length > 0 && (
          <table className="documents">
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Type</th>
                <th scope="col">Chunks</th>
                <th scope="col">Added</th>
                <th scope="col"><span className="visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((document) => (
                <tr key={document.id}>
                  <td className="documents__name">{document.filename}</td>
                  <td>{document.source_type}</td>
                  <td>{document.chunk_count}</td>
                  <td>{new Date(document.ingested_at).toLocaleString()}</td>
                  <td>
                    {document.deletable ? (
                      <button
                        type="button"
                        className="button button--secondary button--small"
                        disabled={deletingId !== null}
                        aria-label={`Delete ${document.filename}`}
                        onClick={() => void onDelete(document.id, document.filename)}
                      >
                        {deletingId === document.id ? "Deleting…" : "Delete"}
                      </button>
                    ) : (
                      <span className="hint" title="Added by server-side ingestion">Managed</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {total > PAGE_SIZE && (
          <nav className="pager" aria-label="Document pages">
            <button type="button" className="button button--secondary button--small" disabled={offset === 0 || loading}
                    onClick={() => void load(Math.max(0, offset - PAGE_SIZE))}>
              Previous
            </button>
            <span className="hint">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
            </span>
            <button type="button" className="button button--secondary button--small"
                    disabled={offset + PAGE_SIZE >= total || loading} onClick={() => void load(offset + PAGE_SIZE)}>
              Next
            </button>
          </nav>
        )}
      </section>
    </main>
  );
}
