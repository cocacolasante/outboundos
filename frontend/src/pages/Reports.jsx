import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  getReportActivities,
  getReportDeals,
  getReportOverview,
} from '../api/reports.js';
import { STAGES, fmtAmount } from './Opportunities.jsx';
import { formatPercent as pct, formatDate as fmtDate } from '../utils/format.js';
import { LoadingCards, ErrorState } from '../components/states.jsx';

const STAGE_LABEL = Object.fromEntries(STAGES.map((s) => [s.value, s.label]));

function isoDaysAgo(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

const PRESETS = [
  { key: '30', label: 'Last 30 days', start: () => isoDaysAgo(30) },
  { key: '90', label: 'Last 90 days', start: () => isoDaysAgo(90) },
  { key: '365', label: 'Last 12 months', start: () => isoDaysAgo(365) },
  { key: 'ytd', label: 'Year to date', start: () => `${new Date().getFullYear()}-01-01` },
  { key: 'all', label: 'All time', start: () => '2000-01-01' },
];

// ---- CSV export -----------------------------------------------------------

function csvCell(v) {
  if (v == null) return '';
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadCsv(filename, headers, rows) {
  const lines = [headers.map(csvCell).join(',')];
  for (const r of rows) lines.push(r.map(csvCell).join(','));
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ---- small presentational bits -------------------------------------------

function Kpi({ label, value, sub, accent }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-4" data-testid={`kpi-${label.toLowerCase().replace(/[^a-z]+/g, '-')}`}>
      <div className="text-xs text-slate-500 uppercase tracking-wide mb-1">{label}</div>
      <div className={`text-2xl font-bold ${accent || 'text-slate-900'}`}>{value}</div>
      {sub && <div className="text-xs text-slate-400 mt-0.5">{sub}</div>}
    </div>
  );
}

function Card({ title, action, children, testid }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-5" data-testid={testid}>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-base font-semibold text-slate-900 m-0">{title}</h2>
        {action}
      </div>
      {children}
    </div>
  );
}

function ExportBtn({ onClick, disabled }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      data-testid="export-csv-btn"
      className="text-xs px-2.5 py-1 rounded-lg border border-slate-300 text-slate-600 hover:bg-slate-50 disabled:opacity-40"
    >
      Export CSV
    </button>
  );
}

// ---- detail tables --------------------------------------------------------

const DEAL_TABS = [
  { key: 'won', label: 'Closed won' },
  { key: 'lost', label: 'Closed lost' },
  { key: 'open', label: 'Open pipeline' },
];

function DealsTable({ range }) {
  const [outcome, setOutcome] = useState('won');
  const { data } = useQuery({
    queryKey: ['report-deals', outcome, range.start, range.end],
    queryFn: () => getReportDeals({ outcome, start: range.start, end: range.end }),
  });
  const items = data?.items || [];

  const exportCsv = () => {
    downloadCsv(
      `deals-${outcome}-${range.start}_${range.end}.csv`,
      ['Name', 'Company', 'Contact', 'Email', 'Stage', 'Amount', 'Weighted', 'Created', 'Closed', 'Age (days)', 'Loss reason'],
      items.map((d) => [
        d.name, d.company, d.contact_name, d.email, STAGE_LABEL[d.stage] || d.stage,
        d.amount ?? '', d.weighted_amount, fmtDate(d.created_at),
        d.closed_at ? fmtDate(d.closed_at) : '', d.age_days ?? '', d.loss_reason ?? '',
      ]),
    );
  };

  return (
    <Card
      title="Deal detail"
      testid="deals-detail"
      action={<ExportBtn onClick={exportCsv} disabled={!items.length} />}
    >
      <div className="flex gap-1.5 mb-3">
        {DEAL_TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            data-testid={`deal-tab-${t.key}`}
            aria-pressed={outcome === t.key}
            onClick={() => setOutcome(t.key)}
            className={`px-3 py-1 text-sm rounded-lg border ${
              outcome === t.key
                ? 'bg-brand-600 text-white border-brand-600'
                : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {items.length === 0 ? (
        <p className="text-sm text-slate-400 py-4 text-center m-0" data-testid="deals-empty">
          No deals in this view.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="deals-table">
            <thead>
              <tr className="text-xs text-slate-500 uppercase border-b border-slate-200">
                <th className="text-left py-2 pr-3">Deal</th>
                <th className="text-left py-2 pr-3">Stage</th>
                <th className="text-right py-2 pr-3">Amount</th>
                <th className="text-right py-2 pr-3">Weighted</th>
                <th className="text-left py-2 pr-3">{outcome === 'open' ? 'Created' : 'Closed'}</th>
                <th className="text-right py-2 pr-3">Age</th>
                {outcome === 'lost' && <th className="text-left py-2">Reason</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((d) => (
                <tr key={d.id} className="border-b border-slate-100">
                  <td className="py-2 pr-3">
                    <div className="font-medium text-slate-800">{d.name}</div>
                    <div className="text-xs text-slate-400">{d.company || d.email || '—'}</div>
                  </td>
                  <td className="py-2 pr-3 text-slate-600">{STAGE_LABEL[d.stage] || d.stage}</td>
                  <td className="py-2 pr-3 text-right tabular">{fmtAmount(d.amount)}</td>
                  <td className="py-2 pr-3 text-right tabular text-slate-500">{fmtAmount(d.weighted_amount)}</td>
                  <td className="py-2 pr-3 text-slate-600 tabular">
                    {fmtDate(outcome === 'open' ? d.created_at : d.closed_at)}
                  </td>
                  <td className="py-2 pr-3 text-right tabular text-slate-500">{d.age_days ?? '—'}</td>
                  {outcome === 'lost' && <td className="py-2 text-slate-600">{d.loss_reason || '—'}</td>}
                </tr>
              ))}
            </tbody>
          </table>
          {data?.truncated && (
            <p className="text-xs text-amber-600 mt-2">
              Showing the first {items.length} of {data.count}. Narrow the date range to see the rest.
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

function ActivitiesTable({ range }) {
  const { data } = useQuery({
    queryKey: ['report-activities', range.start, range.end],
    queryFn: () => getReportActivities({ start: range.start, end: range.end }),
  });
  const items = data?.items || [];

  const exportCsv = () => {
    downloadCsv(
      `activities-${range.start}_${range.end}.csv`,
      ['When', 'Type', 'Direction', 'Subject', 'Contact', 'Deal', 'Logged by', 'Sentiment'],
      items.map((a) => [
        fmtDate(a.occurred_at), a.activity_type, a.direction ?? '', a.subject,
        a.contact ?? '', a.opportunity_name ?? '',
        a.is_agent_generated ? 'Agent' : 'User', a.sentiment ?? '',
      ]),
    );
  };

  return (
    <Card
      title="Activity detail"
      testid="activities-detail"
      action={<ExportBtn onClick={exportCsv} disabled={!items.length} />}
    >
      {items.length === 0 ? (
        <p className="text-sm text-slate-400 py-4 text-center m-0" data-testid="activities-empty">
          No activities logged in this window.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="activities-table">
            <thead>
              <tr className="text-xs text-slate-500 uppercase border-b border-slate-200">
                <th className="text-left py-2 pr-3">When</th>
                <th className="text-left py-2 pr-3">Type</th>
                <th className="text-left py-2 pr-3">Subject</th>
                <th className="text-left py-2 pr-3">Contact / deal</th>
                <th className="text-left py-2">By</th>
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id} className="border-b border-slate-100">
                  <td className="py-2 pr-3 text-slate-600 tabular">{fmtDate(a.occurred_at)}</td>
                  <td className="py-2 pr-3">
                    <span className="capitalize text-slate-700">{a.activity_type}</span>
                    {a.direction && <span className="text-xs text-slate-400"> · {a.direction}</span>}
                  </td>
                  <td className="py-2 pr-3 text-slate-700 max-w-[280px] truncate" title={a.subject}>{a.subject}</td>
                  <td className="py-2 pr-3 text-slate-500">{a.opportunity_name || a.contact || '—'}</td>
                  <td className="py-2">
                    {a.is_agent_generated
                      ? <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-violet-100 text-violet-700">AI</span>
                      : <span className="text-xs text-slate-400">User</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {data?.truncated && (
            <p className="text-xs text-amber-600 mt-2">
              Showing the first {items.length} of {data.count}.
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

// ---- page -----------------------------------------------------------------

export default function Reports() {
  const [preset, setPreset] = useState('90');
  const [customStart, setCustomStart] = useState('');
  const [customEnd, setCustomEnd] = useState('');

  const range = useMemo(() => {
    if (preset === 'custom') {
      return {
        start: customStart || isoDaysAgo(90),
        end: customEnd || isoDaysAgo(0),
      };
    }
    const p = PRESETS.find((x) => x.key === preset) || PRESETS[1];
    return { start: p.start(), end: isoDaysAgo(0) };
  }, [preset, customStart, customEnd]);

  const { data: overview, isLoading, error, refetch } = useQuery({
    queryKey: ['report-overview', range.start, range.end],
    queryFn: () => getReportOverview({ start: range.start, end: range.end }),
  });

  const k = overview?.kpis;
  const monthly = overview?.won_lost_monthly || [];
  const chartData = monthly.map((m) => ({
    month: m.month, Won: m.won_value, Lost: m.lost_value,
  }));

  return (
    <div data-testid="reports-page" className="p-6 max-w-7xl mx-auto">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-1">
        <h1 className="text-2xl font-bold text-slate-900 m-0">Reports</h1>
        <div className="flex flex-wrap items-center gap-1.5" data-testid="date-presets">
          {PRESETS.map((p) => (
            <button
              key={p.key}
              type="button"
              data-testid={`preset-${p.key}`}
              aria-pressed={preset === p.key}
              onClick={() => setPreset(p.key)}
              className={`px-2.5 py-1 text-xs rounded-lg border ${
                preset === p.key
                  ? 'bg-brand-600 text-white border-brand-600'
                  : 'bg-white text-slate-600 border-slate-300 hover:bg-slate-50'
              }`}
            >
              {p.label}
            </button>
          ))}
          <span className="flex items-center gap-1 ml-1">
            <input
              type="date"
              data-testid="custom-start"
              value={customStart}
              onChange={(e) => { setCustomStart(e.target.value); setPreset('custom'); }}
              className="border border-slate-300 rounded-lg px-2 py-1 text-xs"
            />
            <span className="text-slate-400 text-xs">to</span>
            <input
              type="date"
              data-testid="custom-end"
              value={customEnd}
              onChange={(e) => { setCustomEnd(e.target.value); setPreset('custom'); }}
              className="border border-slate-300 rounded-lg px-2 py-1 text-xs"
            />
          </span>
        </div>
      </div>
      <p className="text-sm text-slate-500 mt-0 mb-5">
        Sales performance for {range.start} → {range.end}. Won/lost figures
        are by close date; pipeline + forecast are a live snapshot.
      </p>

      {isLoading && <LoadingCards count={4} testId="reports-loading" />}
      {error && (
        <ErrorState message="Couldn't load reports." onRetry={() => refetch()} testId="reports-error" />
      )}

      {k && (
        <>
          {/* KPI cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5" data-testid="kpi-grid">
            <Kpi label="Won" value={fmtAmount(k.won_value)} sub={`${k.won_count} deal${k.won_count === 1 ? '' : 's'}`} accent="text-emerald-600" />
            <Kpi label="Lost" value={fmtAmount(k.lost_value)} sub={`${k.lost_count} deal${k.lost_count === 1 ? '' : 's'}`} accent="text-red-600" />
            <Kpi label="Win rate" value={pct(k.win_rate)} sub="of closed deals" />
            <Kpi label="Avg deal size" value={fmtAmount(k.avg_deal_size)} sub="won deals" />
            <Kpi label="Avg sales cycle" value={k.avg_sales_cycle_days != null ? `${k.avg_sales_cycle_days} days` : '—'} sub="create → won" />
            <Kpi label="Open pipeline" value={fmtAmount(k.open_value)} sub={`${k.open_count} open · ${fmtAmount(k.open_weighted_value)} weighted`} accent="text-brand-600" />
            <Kpi label="New leads" value={k.new_leads} sub={`${k.conversions} converted`} />
            <Kpi label="Activities" value={k.activities_logged} sub={`${k.deals_created} deals created`} />
          </div>

          {/* Trend + funnel */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 mb-4">
            <div className="lg:col-span-2">
              <Card title="Won vs lost by month" testid="trend-card">
                {chartData.length === 0 ? (
                  <p className="text-sm text-slate-400 py-8 text-center m-0">No data.</p>
                ) : (
                  <ResponsiveContainer width="100%" height={260}>
                    <BarChart data={chartData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                      <XAxis dataKey="month" fontSize={11} />
                      <YAxis fontSize={11} tickFormatter={(v) => fmtAmount(v)} width={70} />
                      <Tooltip formatter={(v) => fmtAmount(v)} />
                      <Legend />
                      <Bar dataKey="Won" fill="#16a34a" />
                      <Bar dataKey="Lost" fill="#dc2626" />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </Card>
            </div>
            <Card title="Conversion funnel" testid="funnel-card">
              <FunnelRows funnel={overview.funnel} />
            </Card>
          </div>

          {/* Pipeline + forecast */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
            <Card title="Pipeline by stage (open)" testid="pipeline-card">
              <SimpleTable
                head={['Stage', 'Deals', 'Value', 'Weighted']}
                rows={overview.pipeline_by_stage.map((s) => [
                  STAGE_LABEL[s.stage] || s.stage, s.count, fmtAmount(s.total_amount), fmtAmount(s.weighted_amount),
                ])}
                empty="No open deals."
              />
            </Card>
            <Card title="Forecast by close month" testid="forecast-card">
              <SimpleTable
                head={['Month', 'Deals', 'Value', 'Weighted']}
                rows={overview.forecast.map((f) => [
                  f.month === 'unscheduled' ? 'No close date' : f.month,
                  f.count, fmtAmount(f.total_amount), fmtAmount(f.weighted_amount),
                ])}
                empty="No open deals to forecast."
              />
            </Card>
          </div>

          {/* Loss reasons + activity breakdown */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
            <Card title="Loss reasons" testid="loss-reasons-card">
              <SimpleTable
                head={['Reason', 'Deals', 'Lost value']}
                rows={overview.loss_reasons.map((r) => [r.reason, r.count, fmtAmount(r.value)])}
                empty="No closed-lost deals in this window."
              />
            </Card>
            <Card title="Activity breakdown" testid="activity-breakdown-card">
              <ActivityBreakdown breakdown={overview.activity_breakdown} />
            </Card>
          </div>

          {/* Detail tables */}
          <div className="grid grid-cols-1 gap-4">
            <DealsTable range={range} />
            <ActivitiesTable range={range} />
          </div>
        </>
      )}
    </div>
  );
}

function FunnelRows({ funnel }) {
  if (!funnel) return null;
  const steps = [
    { label: 'New leads', value: funnel.new_leads },
    { label: 'Opportunities created', value: funnel.opportunities_created, rate: funnel.lead_to_opp_rate, of: 'of leads' },
    { label: 'Won', value: funnel.won, rate: funnel.opp_to_won_rate, of: 'of opportunities' },
  ];
  return (
    <div className="space-y-2" data-testid="funnel-rows">
      {steps.map((s) => (
        <div key={s.label} className="flex items-center justify-between border-b border-slate-100 pb-2 last:border-0">
          <span className="text-sm text-slate-600">{s.label}</span>
          <span className="text-sm">
            <span className="font-semibold text-slate-800">{s.value}</span>
            {s.rate != null && <span className="text-xs text-slate-400 ml-2">{pct(s.rate)} {s.of}</span>}
          </span>
        </div>
      ))}
    </div>
  );
}

function ActivityBreakdown({ breakdown }) {
  if (!breakdown || breakdown.total === 0) {
    return <p className="text-sm text-slate-400 py-4 text-center m-0">No activities in this window.</p>;
  }
  const types = Object.entries(breakdown.by_type);
  return (
    <div data-testid="activity-breakdown">
      <div className="flex flex-wrap gap-2 mb-3">
        {types.map(([t, n]) => (
          <span key={t} className="px-2 py-0.5 rounded-full text-xs bg-slate-100 text-slate-600 capitalize">
            {t}: <span className="font-semibold">{n}</span>
          </span>
        ))}
      </div>
      <div className="text-sm text-slate-600 space-y-1">
        <div>Total: <span className="font-semibold text-slate-800">{breakdown.total}</span></div>
        {Object.entries(breakdown.by_direction).length > 0 && (
          <div>
            {Object.entries(breakdown.by_direction).map(([d, n]) => (
              <span key={d} className="mr-3 capitalize">{d}: {n}</span>
            ))}
          </div>
        )}
        <div className="text-xs text-slate-400">
          {breakdown.human_logged} logged by you · {breakdown.agent_generated} by the agent
        </div>
      </div>
    </div>
  );
}

function SimpleTable({ head, rows, empty }) {
  if (!rows.length) {
    return <p className="text-sm text-slate-400 py-4 text-center m-0">{empty}</p>;
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-xs text-slate-500 uppercase border-b border-slate-200">
          {head.map((h, i) => (
            <th key={h} className={`py-2 ${i === 0 ? 'text-left' : 'text-right'} pr-3`}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, ri) => (
          <tr key={ri} className="border-b border-slate-100">
            {r.map((cell, ci) => (
              <td key={ci} className={`py-2 pr-3 ${ci === 0 ? 'text-left text-slate-700' : 'text-right tabular text-slate-600'}`}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
