import type { KnowledgeDocument } from "../api/types";
import { displayDescription, documentTitle } from "../utils/documents";

/** Title, a short description beneath it, then the file name as secondary metadata. */
export function DocumentName({ document }: { document: KnowledgeDocument }) {
  const description = displayDescription(document.description);
  return (
    <div className="doc-name">
      <span className="doc-title">{documentTitle(document.filename)}</span>
      {description && <p className="doc-description">{description}</p>}
      <span className="doc-file">{document.filename}</span>
    </div>
  );
}
