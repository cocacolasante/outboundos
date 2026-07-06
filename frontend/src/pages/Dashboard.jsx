import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';

import MetricsGrid from '../components/MetricsGrid.jsx';
import { SimpleBarChart } from '../components/charts.jsx';
import { EmptyState, ErrorState, LoadingCards } from '../components/states.jsx';
import { formatCurrency, formatNumber, formatPercent } from '../utils/format.js';
import { getReportOverview } from '../api/reports.js';
import { listCampaigns } from '../api/campaigns.js';
import { listReports, runSavedReport } from '../api/reportBuilder.js';
import { STAGES } from './Opportunities.jsx';

const STAGE_LABEL = Object.fromEntries(STAGES.map((s) => [s.value, s.label]));

function aggregateCampaigns(campaigns) {
  let sent = 0; let opened = 0; let replied = 0;
  for (const c of campaigns) {
    const s = c.stats || {};
    sent += s.sent_count || 0;
    opened += s.opened || 0;
    replied += s.replied || 0;
  }
  return {
    count: campaigns.length,
    sent,
    openRate: sent > 0 ? opened / sent : null,
    replyRate: sent > 0 ? replied / sent : null,
  };
}


function SavedReportResult({ result }) {
  if (!result) return null;
  const cols = result.columns || [];
  const rows = (result.rows || []).slice(0, 50);
  if (rows.length === 0) {
    return <EmptyState title="No rows" hint="This report returned nothing." />;
  }
  return (
    <div className="overflow-auto max-h-[40vh] border-t border-slate-100" data-testid="saved-report-result">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-slate-50">
          <tr className="border-b border-slate-200">
            {cols.map((c) => (
              <th key={c.key} className={`px-4 py-2 text-left text-xs font-semibold text-slate-500 uppercase tracking-wider ${c.type === 'number' ? 'text-right' : ''}`}>
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-slate-100 last:border-0">
              {cols.map((c) => (
                <td key={c.key} className={`px-4 py-2 text-slate-700 ${c.type === 'number' ? 'text-right tabular-nums' : ''}`}>
                  {r[c.key] == null ? '—' : (typeof r[c.key] === 'number' ? formatNumber(r[c.key], { maximumFractionDigits: 2 }) : String(r[c.key]))}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}


export default function Dashboard() {
  const [activeReport, setActiveReport] = useState(null);

  const { data: overview, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard-overview'],
    queryFn: () => getReportOverview(),
  });
  const { data: campaigns = [] } = useQuery({
    queryKey: ['dashboard-campaigns'],
    queryFn: listCampaigns,
  });
  const { data: savedReports = [] } = useQuery({
    queryKey: ['dashboard-saved-reports'],
    queryFn: listReports,
  });

  const runMut = useMutation({
    mutationFn: (id) => runSavedReport(id),
    onSuccess: (data, id) => setActiveReport({ id, result: data }),
  });

  const k = overview?.kpis;
  const kpiTop = k ? [
    { label: 'Won this period', value: formatCurrency(k.won_value) },
    { label: 'Win rate', value: formatPercent(k.win_rate) },
    { label: 'Open pipeline', value: formatCurrency(k.open_value) },
    { label: 'Weighted pipeline', value: formatCurrency(k.open_weighted_value) },
  ] : [];
  const kpiBottom = k ? [
    { label: 'Deals created', value: formatNumber(k.deals_created) },
    { label: 'New leads', value: formatNumber(k.new_leads) },
    { label: 'Conversions', value: formatNumber(k.conversions) },
    { label: 'Activities', value: formatNumber(k.activities_logged) },
  ] : [];

  const pipelineData = (overview?.pipeline_by_stage || []).map((s) => ({
    stage: STAGE_LABEL[s.stage] || s.stage,
    value: s.total_amount,
  }));

  const camp = aggregateCampaigns(campaigns);
  const campaignMetrics = [
    { label: 'Campaigns', value: formatNumber(camp.count) },
    { label: 'Emails sent', value: formatNumber(camp.sent) },
    { label: 'Open rate', value: formatPercent(camp.openRate), tooltip: 'Across all campaigns' },
    { label: 'Reply rate', value: formatPercent(camp.replyRate), tooltip: 'Across all campaigns' },
  ];

  return (
    <div className="p-6 max-w-[100rem] mx-auto" data-testid="dashboard">
      <div className="flex items-center justify-between mb-1">
        <h1 className="text-2xl font-bold text-slate-900 m-0">Dashboard</h1>
        <Link to="/reports/builder" className="text-sm text-brand-600 hover:underline">
          Build a custom report →
        </Link>
      </div>
      <p className="text-sm text-slate-500 mb-5">
        Pipeline health, outreach performance, and your saved reports — one view.
      </p>

      {error ? (
        <ErrorState message="Couldn't load the dashboard." onRetry={refetch} />
      ) : isLoading ? (
        <LoadingCards count={4} />
      ) : (
        <div className="space-y-6">
          {/* CRM KPIs */}
          <section>
            <h2 className="text-sm font-semibold text-slate-700 mb-2">Pipeline (last 90 days)</h2>
            <MetricsGrid metrics={kpiTop} />
            <MetricsGrid metrics={kpiBottom} />
          </section>

          {/* Pipeline by stage */}
          <section className="bg-white rounded-xl border border-slate-200 shadow-sm p-4">
            <h2 className="text-sm font-semibold text-slate-700 mb-3">Open pipeline by stage</h2>
            <SimpleBarChart data={pipelineData} xKey="stage" yKey="value" testId="dashboard-pipeline-chart" />
          </section>

          {/* Campaign snapshot */}
          <section>
            <h2 className="text-sm font-semibold text-slate-700 mb-2">Outreach</h2>
            <MetricsGrid metrics={campaignMetrics} />
          </section>

          {/* Saved reports */}
          <section className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden" data-testid="dashboard-saved-reports">
            <div className="px-4 py-3 border-b border-slate-200 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-700 m-0">Saved reports</h2>
              <Link to="/reports/builder" className="text-xs text-brand-600 hover:underline">Manage →</Link>
            </div>
            {savedReports.length === 0 ? (
              <EmptyState title="No saved reports yet" hint="Build one in the report builder to pin it here." icon="📋" />
            ) : (
              <div className="divide-y divide-slate-100">
                {savedReports.map((r) => (
                  <div key={r.id}>
                    <button
                      type="button"
                      data-testid={`dashboard-report-${r.id}`}
                      onClick={() => runMut.mutate(r.id)}
                      className="w-full text-left px-4 py-3 hover:bg-slate-50 flex items-center justify-between"
                    >
                      <span>
                        <span className="text-sm font-medium text-slate-800">{r.name}</span>
                        <span className="block text-xs text-slate-400">{r.data_source}{r.description ? ` · ${r.description}` : ''}</span>
                      </span>
                      <span className="text-xs text-brand-600">
                        {runMut.isPending && runMut.variables === r.id ? 'Running…' : 'Run ▸'}
                      </span>
                    </button>
                    {activeReport?.id === r.id && <SavedReportResult result={activeReport.result} />}
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
