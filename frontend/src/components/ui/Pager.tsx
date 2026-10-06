/** "1–20 of 45" with Previous / Next. */
export function Pager({ offset, pageSize, total, disabled, label, onPage }: {
  offset: number;
  pageSize: number;
  total: number;
  disabled?: boolean;
  label: string;
  onPage: (offset: number) => void;
}) {
  if (total <= pageSize) return null;
  return (
    <nav className="pager" aria-label={label}>
      <button type="button" className="button button--ghost button--small" disabled={offset === 0 || disabled}
              onClick={() => onPage(Math.max(0, offset - pageSize))}>
        Previous
      </button>
      <span className="pager__range">{offset + 1}–{Math.min(offset + pageSize, total)} of {total}</span>
      <button type="button" className="button button--ghost button--small" disabled={offset + pageSize >= total || disabled}
              onClick={() => onPage(offset + pageSize)}>
        Next
      </button>
    </nav>
  );
}
