import type { Source } from "../api/types";

/** Everything the backend returned about one source. All values come from retrieval metadata. */
export function SourceDetails({ source }: { source: Source }) {
  const rows: Array<[string, string | number | null]> = [
    ["Type", source.source_type],
    ["Page", source.page_number],
    ["Section", source.section],
    ["Document ID", source.doc_id],
    ["Path", source.relative_path],
    ["Chunk", source.chunk_id],
    ["Similarity", source.similarity.toFixed(4)],
    ["Rerank score", source.rerank_score === null ? null : source.rerank_score.toFixed(2)],
  ];
  return (
    <section className="source-details" aria-label={`Source ${source.number} details`}>
      <h4>
        [{source.number}] {source.source}
      </h4>
      <dl>
        {rows
          .filter(([, value]) => value !== null && value !== "")
          .map(([label, value]) => (
            <div key={label} className="source-details__row">
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
      </dl>
      <blockquote className="source-details__text">{source.text}</blockquote>
    </section>
  );
}
