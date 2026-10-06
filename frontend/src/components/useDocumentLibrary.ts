import { useCallback, useEffect, useRef, useState } from "react";

import { listDocuments } from "../api/endpoints";
import type { DocumentList, KnowledgeDocument } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { documentTitle, documentTypeLabel } from "../utils/documents";

const SEARCH_BATCH = 200; // the API's maximum page size

/**
 * The documents the current user may read, one page at a time, plus a search over all of them.
 * The search loads the user's list (which the server already filters) in batches the first time
 * it is used and filters it in the browser; nothing extra is sent to the server.
 */
export function useDocumentLibrary(pageSize: number, onError: (error: unknown) => void) {
  const { token } = useAuth();
  const [page, setPage] = useState<DocumentList | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [all, setAll] = useState<KnowledgeDocument[] | null>(null);
  const [searching, setSearching] = useState(false);
  const errorRef = useRef(onError);
  errorRef.current = onError;

  const load = useCallback(
    async (nextOffset: number) => {
      if (!token) return;
      setLoading(true);
      try {
        setPage(await listDocuments(token, pageSize, nextOffset));
        setOffset(nextOffset);
      } catch (caught) {
        errorRef.current(caught);
      } finally {
        setLoading(false);
      }
    },
    [token, pageSize],
  );

  useEffect(() => {
    void load(0);
  }, [load]);

  /** Reload the current page (and drop the search cache) after a change. */
  const refresh = useCallback(
    async (nextOffset?: number) => {
      setAll(null);
      await load(nextOffset ?? offset);
    },
    [load, offset],
  );

  const term = query.trim().toLowerCase();
  useEffect(() => {
    if (!term || all !== null || !token) return;
    let cancelled = false;
    setSearching(true);
    (async () => {
      try {
        const items: KnowledgeDocument[] = [];
        for (let next = 0; ; next += SEARCH_BATCH) {
          const batch = await listDocuments(token, SEARCH_BATCH, next);
          items.push(...batch.items);
          if (cancelled || next + SEARCH_BATCH >= batch.total) break;
        }
        if (!cancelled) setAll(items);
      } catch (caught) {
        if (!cancelled) errorRef.current(caught);
      } finally {
        if (!cancelled) setSearching(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [term, all, token]);

  const matches = term && all
    ? all.filter((d) =>
        [d.filename, documentTitle(d.filename), documentTypeLabel(d.source_type, d.filename), d.description ?? ""]
          .some((text) => text.toLowerCase().includes(term)))
    : null;

  return { page, offset, loading, load, refresh, query, setQuery, searching: Boolean(term) && searching, matches };
}
