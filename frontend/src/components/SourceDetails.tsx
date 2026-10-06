import type { Source } from "../api/types";

/** The passage a source contributed, with the details people need; retrieval internals stay folded away. */
export function SourceDetails({ source, cited, onClose }: { source: Source; cited: boolean; onClose: () => void }) {
  const facts: Array<[string, string | number | null]> = [
    ["Section", source.section],
    ["Page", source.page_number],
    ["File", source.source],
    ["Cited in the answer", cited ? "Yes" : null],
  ];
  const technical: Array<[string, string | number | null]> = [
    ["Document ID", source.doc_id],
    ["Path", source.relative_path],
    ["Passage", source.chunk_id],
    ["Similarity", source.similarity.toFixed(4)],
    ["Relevance score", source.rerank_score === null ? null : source.rerank_score.toFixed(2)],
  ];
  const rows = (items: typeof facts) =>
    items
      .filter(([, value]) => value !== null && value !== "")
      .map(([label, value]) => (
        <div key={label} className="facts__row">
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ));

  return (
    <section className="source-details" aria-label={`Source ${source.number} details`}>
      <blockquote className="source-details__text">{source.text}</blockquote>
      <dl className="facts">{rows(facts)}</dl>
      {/* Native <details>: closed until the user opens it; it is re-created closed whenever the source re-opens. */}
      <details className="source-details__technical">
        <summary>Technical details</summary>
        <dl className="facts facts--technical">{rows(technical)}</dl>
      </details>
      <div className="source-details__actions">
        <button type="button" className="button button--ghost button--small" onClick={onClose}>
          Close source {source.number}
        </button>
      </div>
    </section>
  );
}
