/**
 * Data table primitive (Phase 2, UI refinement).  Dense, hairline-separated
 * rows (no zebra), sticky header, optional sortable headers, right-aligned +
 * tabular numerics for number columns.  Replaces the per-page <table> markup.
 *
 * columns: [{ key, label, align?: 'left'|'right', numeric?, sortable?, render?(row) }]
 * rows:    array; getRowKey(row) -> key; onRowClick?(row)
 * sort:    { key, dir: 'asc'|'desc' } | null; onSort?(key)
 */
export function Table({
  columns, rows, getRowKey = (r, i) => r.id ?? i, onRowClick,
  sort = null, onSort, empty = 'No rows.', testId = 'table', className = '',
}) {
  function ariaSort(col) {
    if (!col.sortable || sort?.key !== col.key) return col.sortable ? 'none' : undefined;
    return sort.dir === 'asc' ? 'ascending' : 'descending';
  }

  return (
    <div className={`overflow-auto rounded-card border border-slate-200 ${className}`}>
      <table className="w-full text-sm" data-testid={testId}>
        <thead className="sticky top-0 z-10 bg-slate-50">
          <tr className="border-b border-slate-200">
            {columns.map((c) => {
              const alignRight = c.align === 'right' || c.numeric;
              const isSorted = sort?.key === c.key;
              return (
                <th
                  key={c.key}
                  aria-sort={ariaSort(c)}
                  className={`px-4 py-2.5 text-xs font-semibold text-slate-500 uppercase tracking-wider
                    ${alignRight ? 'text-right' : 'text-left'}`}
                >
                  {c.sortable ? (
                    <button
                      type="button"
                      data-testid={`${testId}-sort-${c.key}`}
                      onClick={() => onSort?.(c.key)}
                      className={`inline-flex items-center gap-1 hover:text-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded
                        ${alignRight ? 'flex-row-reverse' : ''}`}
                    >
                      {c.label}
                      <span aria-hidden="true" className={isSorted ? 'text-brand-600' : 'text-slate-300'}>
                        {isSorted ? (sort.dir === 'asc' ? '▲' : '▼') : '↕'}
                      </span>
                    </button>
                  ) : c.label}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-4 py-10 text-center text-sm text-slate-400" data-testid={`${testId}-empty`}>
                {empty}
              </td>
            </tr>
          ) : rows.map((row, i) => (
            <tr
              key={getRowKey(row, i)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              data-testid={`${testId}-row`}
              className={`border-b border-slate-100 last:border-0 ${onRowClick ? 'hover:bg-slate-50 cursor-pointer' : ''}`}
            >
              {columns.map((c) => {
                const alignRight = c.align === 'right' || c.numeric;
                return (
                  <td
                    key={c.key}
                    className={`px-4 py-2.5 text-slate-700 ${alignRight ? 'text-right tabular' : 'text-left'}`}
                  >
                    {c.render ? c.render(row) : row[c.key]}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
