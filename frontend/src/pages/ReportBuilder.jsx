import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  createReport, deleteReport, duplicateReport, getReportMetadata,
  listReports, runAdhocReport, updateReport,
} from '../api/reportBuilder.js';
import { useToast } from '../components/Toast.jsx';
import { SimpleBarChart, SimpleLineChart, SimplePieChart } from '../components/charts.jsx';
import { Button } from '../components/ui.jsx';
import { formatNumber } from '../utils/format.js';

const OP_LABELS = {
  equals: 'is', not_equals: 'is not', contains: 'contains', not_contains: 'does not contain',
  starts_with: 'starts with', in: 'is any of', is_empty: 'is empty', is_not_empty: 'is not empty',
  gt: '>', gte: '≥', lt: '<', lte: '≤', between: 'between',
  before: 'before', after: 'after', relative_range: 'in the',
};
const NO_VALUE_OPS = new Set(['is_empty', 'is_not_empty']);

function emptyDef() {
  return { id: null, name: '', description: '', data_source: '', mode: 'table',
    columns: [], filters: [], group_by: [], aggregates: [], sort: [] };
}

function fmtCell(v) {
  if (v == null) return '—';
  if (typeof v === 'number') return formatNumber(v, { maximumFractionDigits: 2 });
  return String(v);
}

function toCsv(columns, rows) {
  const head = columns.map((c) => c.label).join(',');
  const lines = rows.map((r) => columns.map((c) => {
    const v = r[c.key];
    const s = v == null ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }).join(','));
  return [head, ...lines].join('\n');
}


function FilterValueInput({ fieldMeta, op, value, relativeRanges, onChange }) {
  if (!fieldMeta || NO_VALUE_OPS.has(op)) return null;
  const t = fieldMeta.type;
  if (op === 'relative_range') {
    return (
      <select data-testid="filter-value" value={value || ''} onChange={(e) => onChange(e.target.value)}
        className="px-2 py-1.5 border border-slate-300 rounded-lg text-sm">
        <option value="">—</option>
        {relativeRanges.map((r) => <option key={r} value={r}>{r.replace(/_/g, ' ')}</option>)}
      </select>
    );
  }
  if (op === 'between') {
    const arr = Array.isArray(value) ? value : ['', ''];
    const itype = t === 'number' ? 'number' : (t === 'date' || t === 'datetime') ? 'date' : 'text';
    return (
      <span className="inline-flex gap-1">
        <input data-testid="filter-value" type={itype} value={arr[0] ?? ''} onChange={(e) => onChange([e.target.value, arr[1] ?? ''])}
          className="w-24 px-2 py-1.5 border border-slate-300 rounded-lg text-sm" />
        <input data-testid="filter-value-2" type={itype} value={arr[1] ?? ''} onChange={(e) => onChange([arr[0] ?? '', e.target.value])}
          className="w-24 px-2 py-1.5 border border-slate-300 rounded-lg text-sm" />
      </span>
    );
  }
  if (t === 'enum' && (op === 'equals' || op === 'not_equals')) {
    return (
      <select data-testid="filter-value" value={value || ''} onChange={(e) => onChange(e.target.value)}
        className="px-2 py-1.5 border border-slate-300 rounded-lg text-sm">
        <option value="">—</option>
        {(fieldMeta.enum_values || []).map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
    );
  }
  const itype = t === 'number' ? 'number' : (t === 'date' || t === 'datetime') ? 'date' : 'text';
  return (
    <input data-testid="filter-value" type={itype} value={value ?? ''} onChange={(e) => onChange(e.target.value)}
      placeholder={op === 'in' ? 'comma,separated' : ''}
      className="px-2 py-1.5 border border-slate-300 rounded-lg text-sm" />
  );
}


export default function ReportBuilder() {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: metadata } = useQuery({ queryKey: ['report-metadata'], queryFn: getReportMetadata });
  const { data: saved = [] } = useQuery({ queryKey: ['saved-reports'], queryFn: listReports });

  const [def, setDef] = useState(emptyDef());
  const [result, setResult] = useState(null);
  const [chartType, setChartType] = useState('bar');

  const objects = metadata?.objects ?? [];
  const relativeRanges = metadata?.relative_ranges ?? [];
  const obj = objects.find((o) => o.key === def.data_source) || null;
  const fieldsByKey = useMemo(
    () => Object.fromEntries((obj?.fields || []).map((f) => [f.key, f])),
    [obj],
  );

  const patch = (changes) => setDef((d) => ({ ...d, ...changes }));

  function pickSource(key) {
    const o = objects.find((x) => x.key === key);
    setDef({
      ...emptyDef(), id: def.id, name: def.name, description: def.description,
      data_source: key, columns: o ? [...o.default_columns] : [],
    });
    setResult(null);
  }

  function buildDefinition() {
    const base = { filters: def.filters.filter((f) => f.field), sort: def.sort.filter((s) => s.field) };
    if (def.mode === 'summary') {
      return { ...base, group_by: def.group_by, aggregates: def.aggregates.filter((a) => a.fn) };
    }
    return { ...base, columns: def.columns };
  }

  const runMut = useMutation({
    mutationFn: () => runAdhocReport({ data_source: def.data_source, definition: buildDefinition() }),
    onSuccess: (data) => setResult(data),
    onError: (e) => toast.error(e?.response?.data?.detail || 'Report failed'),
  });

  const saveMut = useMutation({
    mutationFn: () => {
      const payload = {
        name: def.name.trim(), description: def.description || null,
        data_source: def.data_source, definition: buildDefinition(),
      };
      return def.id ? updateReport(def.id, payload) : createReport(payload);
    },
    onSuccess: (data) => {
      patch({ id: data.id });
      queryClient.invalidateQueries({ queryKey: ['saved-reports'] });
      toast.success('Report saved');
    },
    onError: (e) => toast.error(e?.response?.data?.detail || 'Save failed'),
  });

  const dupMut = useMutation({
    mutationFn: () => duplicateReport(def.id),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['saved-reports'] }); toast.success('Duplicated'); },
  });
  const delMut = useMutation({
    mutationFn: () => deleteReport(def.id),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['saved-reports'] }); setDef(emptyDef()); setResult(null); toast.success('Deleted'); },
  });

  function loadSaved(r) {
    const d = r.definition || {};
    setDef({
      id: r.id, name: r.name, description: r.description || '', data_source: r.data_source,
      mode: (d.group_by?.length || d.aggregates?.length) ? 'summary' : 'table',
      columns: d.columns || [], filters: d.filters || [], group_by: d.group_by || [],
      aggregates: d.aggregates || [], sort: d.sort || [],
    });
    setResult(null);
  }

  function exportCsv() {
    if (!result?.rows?.length) return;
    const blob = new Blob([toCsv(result.columns, result.rows)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `${(def.name || 'report').replace(/\s+/g, '-')}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const numericAggKeys = (result?.columns || []).filter((c) => c.type === 'number').map((c) => c.key);
  const groupKey = def.mode === 'summary' ? def.group_by[0] : null;

  return (
    <div className="p-6 max-w-[100rem] mx-auto" data-testid="report-builder">
      <div className="flex items-center justify-between mb-5">
        <h1 className="text-2xl font-bold text-slate-900 m-0">Report builder</h1>
        <Button onClick={() => { setDef(emptyDef()); setResult(null); }} data-testid="new-report-btn">
          + New report
        </Button>
      </div>

      <div className="grid grid-cols-[240px_1fr] gap-5">
        {/* Saved reports rail */}
        <aside className="space-y-1" data-testid="saved-reports">
          <div className="text-xs font-semibold text-slate-500 uppercase tracking-wide px-1 mb-1">Saved reports</div>
          {saved.length === 0 ? (
            <div className="text-xs text-slate-400 px-1 py-2">No saved reports yet.</div>
          ) : saved.map((r) => (
            <button key={r.id} type="button" onClick={() => loadSaved(r)}
              data-testid={`saved-report-${r.id}`}
              className={`w-full text-left px-2.5 py-2 rounded-lg text-sm hover:bg-slate-100 ${
                def.id === r.id ? 'bg-brand-50 text-brand-700 font-medium' : 'text-slate-700'}`}>
              {r.name}
              <span className="block text-[11px] text-slate-400">{r.data_source}</span>
            </button>
          ))}
        </aside>

        {/* Builder */}
        <div className="space-y-4">
          <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4 space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-semibold text-slate-600 mb-1">Report name</label>
                <input value={def.name} onChange={(e) => patch({ name: e.target.value })}
                  data-testid="report-name" placeholder="Open deals by stage"
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm" />
              </div>
              <div>
                <label className="block text-xs font-semibold text-slate-600 mb-1">Data source</label>
                <select value={def.data_source} onChange={(e) => pickSource(e.target.value)}
                  data-testid="data-source-select"
                  className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm bg-white">
                  <option value="">Choose…</option>
                  {objects.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
                </select>
              </div>
            </div>

            {obj && (
              <>
                {/* Mode */}
                <div className="inline-flex rounded-lg border border-slate-300 overflow-hidden">
                  {['table', 'summary'].map((m) => (
                    <button key={m} type="button" onClick={() => patch({ mode: m })}
                      data-testid={`mode-${m}`} aria-pressed={def.mode === m}
                      className={`px-3 py-1.5 text-sm font-medium ${def.mode === m ? 'bg-brand-600 text-white' : 'bg-white text-slate-600 hover:bg-slate-50'}`}>
                      {m === 'table' ? 'Table' : 'Summary (grouped)'}
                    </button>
                  ))}
                </div>

                {def.mode === 'table' ? (
                  <fieldset>
                    <legend className="text-xs font-semibold text-slate-600 mb-1">Columns</legend>
                    <div className="flex flex-wrap gap-1.5">
                      {obj.fields.map((f) => {
                        const on = def.columns.includes(f.key);
                        return (
                          <button key={f.key} type="button"
                            data-testid={`column-toggle-${f.key}`}
                            aria-pressed={on}
                            aria-label={`Column ${f.label}${on ? ' (selected)' : ''}`}
                            onClick={() => patch({ columns: on ? def.columns.filter((c) => c !== f.key) : [...def.columns, f.key] })}
                            className={`px-2 py-1 rounded-md text-xs border focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${on ? 'bg-brand-50 border-brand-300 text-brand-700' : 'bg-white border-slate-300 text-slate-600'}`}>
                            {f.label}
                          </button>
                        );
                      })}
                    </div>
                  </fieldset>
                ) : (
                  <div className="space-y-3">
                    <fieldset>
                      <legend className="text-xs font-semibold text-slate-600 mb-1">Group by</legend>
                      <div className="flex flex-wrap gap-1.5">
                        {obj.fields.filter((f) => f.groupable).map((f) => {
                          const on = def.group_by.includes(f.key);
                          return (
                            <button key={f.key} type="button"
                              data-testid={`groupby-toggle-${f.key}`}
                              aria-pressed={on}
                              aria-label={`Group by ${f.label}${on ? ' (selected)' : ''}`}
                              onClick={() => patch({ group_by: on ? def.group_by.filter((c) => c !== f.key) : [...def.group_by, f.key] })}
                              className={`px-2 py-1 rounded-md text-xs border focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 ${on ? 'bg-violet-50 border-violet-300 text-violet-700' : 'bg-white border-slate-300 text-slate-600'}`}>
                              {f.label}
                            </button>
                          );
                        })}
                      </div>
                    </fieldset>
                    <fieldset>
                      <legend className="text-xs font-semibold text-slate-600 mb-1">Aggregates</legend>
                      {def.aggregates.map((a, i) => (
                        <div key={i} className="flex items-center gap-2 mb-1.5">
                          <select data-testid={`agg-fn-${i}`} value={a.fn || ''}
                            onChange={(e) => patch({ aggregates: def.aggregates.map((x, j) => j === i ? { ...x, fn: e.target.value } : x) })}
                            className="px-2 py-1.5 border border-slate-300 rounded-lg text-sm">
                            <option value="count">count</option>
                            <option value="sum">sum</option>
                            <option value="avg">avg</option>
                            <option value="min">min</option>
                            <option value="max">max</option>
                          </select>
                          {a.fn !== 'count' && (
                            <select data-testid={`agg-field-${i}`} value={a.field || ''}
                              onChange={(e) => patch({ aggregates: def.aggregates.map((x, j) => j === i ? { ...x, field: e.target.value } : x) })}
                              className="px-2 py-1.5 border border-slate-300 rounded-lg text-sm">
                              <option value="">field…</option>
                              {obj.fields.filter((f) => (f.aggregates || []).includes(a.fn)).map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
                            </select>
                          )}
                          <button type="button" aria-label="Remove aggregate"
                            onClick={() => patch({ aggregates: def.aggregates.filter((_, j) => j !== i) })}
                            className="text-slate-400 hover:text-red-600 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded">✕</button>
                        </div>
                      ))}
                      <button type="button" data-testid="add-aggregate-btn"
                        onClick={() => patch({ aggregates: [...def.aggregates, { fn: 'count' }] })}
                        className="text-xs text-brand-600 hover:underline">+ Add aggregate</button>
                    </fieldset>
                  </div>
                )}

                {/* Filters */}
                <fieldset>
                  <legend className="text-xs font-semibold text-slate-600 mb-1">Filters</legend>
                  {def.filters.map((flt, i) => {
                    const fm = fieldsByKey[flt.field];
                    const ops = fm?.operators || [];
                    return (
                      <div key={i} className="flex items-center gap-2 mb-1.5 flex-wrap">
                        <select data-testid={`filter-field-${i}`} value={flt.field || ''}
                          onChange={(e) => patch({ filters: def.filters.map((x, j) => j === i ? { field: e.target.value, op: '', value: '' } : x) })}
                          className="px-2 py-1.5 border border-slate-300 rounded-lg text-sm">
                          <option value="">field…</option>
                          {obj.fields.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
                        </select>
                        <select data-testid={`filter-op-${i}`} value={flt.op || ''} disabled={!fm}
                          onChange={(e) => patch({ filters: def.filters.map((x, j) => j === i ? { ...x, op: e.target.value } : x) })}
                          className="px-2 py-1.5 border border-slate-300 rounded-lg text-sm">
                          <option value="">op…</option>
                          {ops.map((op) => <option key={op} value={op}>{OP_LABELS[op] || op}</option>)}
                        </select>
                        <FilterValueInput fieldMeta={fm} op={flt.op} value={flt.value} relativeRanges={relativeRanges}
                          onChange={(v) => patch({ filters: def.filters.map((x, j) => j === i ? { ...x, value: v } : x) })} />
                        <button type="button" data-testid={`remove-filter-${i}`} aria-label="Remove filter"
                          onClick={() => patch({ filters: def.filters.filter((_, j) => j !== i) })}
                          className="text-slate-400 hover:text-red-600 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 rounded">✕</button>
                      </div>
                    );
                  })}
                  <button type="button" data-testid="add-filter-btn"
                    onClick={() => patch({ filters: [...def.filters, { field: '', op: '', value: '' }] })}
                    className="text-xs text-brand-600 hover:underline">+ Add filter</button>
                </fieldset>

                {/* Actions */}
                <div className="flex items-center gap-2 pt-1">
                  <Button onClick={() => runMut.mutate()} disabled={!def.data_source}
                    loading={runMut.isPending} data-testid="run-btn">
                    {runMut.isPending ? 'Running…' : 'Run'}
                  </Button>
                  <Button variant="secondary" onClick={() => saveMut.mutate()}
                    disabled={!def.name.trim() || !def.data_source} loading={saveMut.isPending}
                    data-testid="save-report-btn">
                    {def.id ? 'Save changes' : 'Save report'}
                  </Button>
                  {def.id && (
                    <>
                      <Button variant="ghost" onClick={() => dupMut.mutate()} data-testid="duplicate-btn">Duplicate</Button>
                      <Button variant="ghost" onClick={() => delMut.mutate()} data-testid="delete-btn"
                        className="text-red-600 hover:bg-red-50 active:bg-red-100">Delete</Button>
                    </>
                  )}
                </div>
              </>
            )}
          </div>

          {/* Results */}
          {result && <Results result={result} chartType={chartType} setChartType={setChartType}
            groupKey={groupKey} numericAggKeys={numericAggKeys} onExport={exportCsv} />}
        </div>
      </div>
    </div>
  );
}


function Results({ result, chartType, setChartType, groupKey, numericAggKeys, onExport }) {
  const { columns, rows, grouped, truncated, row_count: rowCount } = result;
  const showChart = grouped && groupKey && numericAggKeys.length > 0;
  const yKey = numericAggKeys[0];

  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden" data-testid="report-results">
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-slate-200">
        <span className="text-sm font-semibold text-slate-700">
          {rowCount} {rowCount === 1 ? 'row' : 'rows'}{truncated ? ' (truncated)' : ''}
        </span>
        <div className="flex items-center gap-2">
          {showChart && (
            <select value={chartType} onChange={(e) => setChartType(e.target.value)}
              data-testid="chart-type-select"
              className="px-2 py-1 border border-slate-300 rounded-lg text-xs">
              <option value="none">No chart</option>
              <option value="bar">Bar</option>
              <option value="line">Line</option>
              <option value="pie">Pie</option>
            </select>
          )}
          <button type="button" onClick={onExport} disabled={!rows.length}
            data-testid="export-csv-btn"
            className="px-3 py-1.5 text-xs font-medium border border-slate-300 rounded-md text-slate-700 hover:bg-slate-50 disabled:opacity-50">
            Export CSV
          </button>
        </div>
      </div>

      {showChart && chartType !== 'none' && (
        <div className="p-4 border-b border-slate-100" data-testid="report-chart">
          {chartType === 'pie' ? (
            <SimplePieChart data={rows} nameKey={groupKey} valueKey={yKey} />
          ) : chartType === 'line' ? (
            <SimpleLineChart data={rows} xKey={groupKey} yKey={yKey} />
          ) : (
            <SimpleBarChart data={rows} xKey={groupKey} yKey={yKey} />
          )}
        </div>
      )}

      {rows.length === 0 ? (
        <div className="px-4 py-10 text-center text-sm text-slate-400" data-testid="results-empty">No rows match.</div>
      ) : (
        <div className="overflow-auto max-h-[60vh]">
          <table className="w-full text-sm" data-testid="results-table">
            <thead className="sticky top-0 bg-slate-50 z-10">
              <tr className="border-b border-slate-200">
                {columns.map((c) => (
                  <th key={c.key} data-testid={`result-col-${c.key}`}
                    className={`px-4 py-2 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider ${c.type === 'number' ? 'text-right' : ''}`}>
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                  {columns.map((c) => (
                    <td key={c.key} className={`px-4 py-2 text-slate-700 ${c.type === 'number' ? 'text-right tabular-nums' : ''}`}>
                      {fmtCell(r[c.key])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
